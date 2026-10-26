"""ZIP folders for the upload dialog: opened first, uploaded only for the files the person ticks.

  extract   the ZIP, and every ZIP inside it, is unpacked into a staging folder (one per upload, on the shared data volume);
            nothing is read or indexed. The list of files, with their folders, goes to the browser.
  commit    the files the person kept ticked are stored and queued exactly as if they had been chosen one by one
            (api/routers/uploads.py); more can be ticked and committed afterwards.
  discard   closing the dialog removes the folder; one nobody came back to is swept after STAGING_TTL_S (maintenance job).

Files are written to disk one member at a time, so a multi-gigabyte ZIP never sits in memory. Paths are never used to name
files on disk (each file is stored as files/<id>), so a hostile path inside a ZIP cannot write outside the folder.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from rag.errors import PermanentError
from rag.ingestion import protection
from rag.ingestion.formats import SUPPORTED, extension

log = logging.getLogger(__name__)

STAGING_TTL_S = 12 * 3600
MAX_DEPTH = 3  # a ZIP inside a ZIP inside the uploaded ZIP is opened; deeper ones are listed but not opened
MAX_FILES = 20_000
MAX_RATIO = 250
# What operating systems put into archives, never documents: left out without a word.
_SYSTEM_FILES = re.compile(r"(?i)(^|/)(__MACOSX|\.DS_Store|Thumbs\.db|desktop\.ini)(/|$)|(^|/)(~\$|\._)[^/]*$")
_ID = re.compile(r"^[0-9a-f]{32}$")


def staging_root(settings: Any) -> Path:
    return Path(settings.upload_staging_dir or Path(settings.cache_dir) / "upload-staging")


def create(settings: Any) -> tuple[str, Path]:
    base = staging_root(settings)
    base.mkdir(parents=True, exist_ok=True)
    staging_id = uuid.uuid4().hex
    root = base / staging_id
    (root / "files").mkdir(parents=True)
    return staging_id, root


def remove(settings: Any, staging_id: str) -> None:
    if _ID.match(staging_id):
        shutil.rmtree(staging_root(settings) / staging_id, ignore_errors=True)


def sweep(settings: Any, max_age_s: float = STAGING_TTL_S) -> int:
    """Remove staging folders nobody committed or discarded. Returns how many."""
    base = staging_root(settings)
    if not base.is_dir():
        return 0
    removed = 0
    for folder in base.iterdir():
        try:
            if folder.is_dir() and time.time() - folder.stat().st_mtime > max_age_s:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def load(settings: Any, staging_id: str, user_id: str) -> tuple[Path, dict[str, Any]]:
    """The staged upload of this person, or PermanentError (unknown, expired or somebody else's)."""
    if not _ID.match(staging_id):
        raise PermanentError("Unknown upload")
    root = staging_root(settings) / staging_id
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PermanentError("This upload has expired; choose the ZIP again.") from exc
    if manifest.get("owner") != user_id:
        raise PermanentError("Unknown upload")
    return root, manifest


class _Limits:
    def __init__(self, settings: Any) -> None:
        self.member_bytes = settings.upload_max_mb * 1024 * 1024
        self.total_bytes = 4 * settings.upload_zip_max_mb * 1024 * 1024
        self.files = 0
        self.bytes = 0
        self.stopped = False


def extract(settings: Any, source: BinaryIO, zip_name: str, user_id: str) -> tuple[str, list[dict[str, Any]]]:
    """Unpack `source` (a ZIP) into a new staging folder. Returns (staging id, entries).

    Each entry: id, path (inside the ZIP, nested ZIPs shown as folders), name, ext, size, supported, problem. Files with a
    problem were not unpacked (too large, damaged, password-protected, unsafe path) and say why."""
    try:
        zf = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise PermanentError("The ZIP file is damaged.") from exc
    staging_id, root = create(settings)
    entries: list[dict[str, Any]] = []
    try:
        with zf:
            _walk(zf, "", 1, root, _Limits(settings), entries)
        (root / "manifest.json").write_text(
            json.dumps({"owner": user_id, "name": zip_name, "created": time.time(),
                        "files": {e["id"]: {"path": e["path"], "name": e["name"], "size": e["size"], "problem": e["problem"],
                                            "protected": e["protected"]}
                                  for e in entries}}),
            encoding="utf-8")
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return staging_id, entries


def _entry(entries: list[dict[str, Any]], path: str, size: int, problem: str | None,
           protected: str | None = None) -> dict[str, Any]:
    name = PurePosixPath(path).name
    ext = extension(name)
    entry = {"id": f"f{len(entries)}", "path": path, "name": name, "ext": ext, "size": size,
             "supported": not ext or ext in SUPPORTED, "problem": problem, "protected": protected}
    entries.append(entry)
    return entry


def _walk(zf: zipfile.ZipFile, prefix: str, depth: int, root: Path, limits: _Limits, entries: list[dict[str, Any]]) -> None:
    for info in zf.infolist():
        if info.is_dir() or limits.stopped:
            continue
        raw = info.filename.replace("\\", "/")
        if _SYSTEM_FILES.search(raw):
            continue
        inner = PurePosixPath(raw)
        path = f"{prefix}{raw}"
        if inner.is_absolute() or ".." in inner.parts:
            _entry(entries, path.replace("..", "_"), info.file_size, "Unsafe path inside the ZIP; left out.")
            continue
        limits.files += 1
        if limits.files > MAX_FILES:
            _entry(entries, path, info.file_size, f"The ZIP has more than {MAX_FILES:,} files; the rest are not listed.")
            limits.stopped = True
            return
        if info.flag_bits & 0x1:
            _entry(entries, path, info.file_size, protection.ZIP_PASSWORD, protected=protection.ZIP_PASSWORD)
            continue
        if info.file_size > limits.member_bytes:
            _entry(entries, path, info.file_size, f"Larger than {limits.member_bytes // (1024 * 1024)} MB (UPLOAD_MAX_MB).")
            continue
        if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
            _entry(entries, path, info.file_size, "Suspicious compression ratio (possible zip bomb).")
            continue
        limits.bytes += info.file_size
        if limits.bytes > limits.total_bytes:
            _entry(entries, path, info.file_size, "The ZIP expands beyond the size limit; the rest are not listed.")
            limits.stopped = True
            return
        if extension(inner.name) == "zip" and _nested(zf, info, f"{path}/", depth, root, limits, entries):
            continue
        _store(zf, info, path, root, entries)


def _store(zf: zipfile.ZipFile, info: zipfile.ZipInfo, path: str, root: Path, entries: list[dict[str, Any]]) -> None:
    entry = _entry(entries, path, info.file_size, None)
    try:
        with zf.open(info) as src, (root / "files" / entry["id"]).open("wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError, zipfile.LargeZipFile) as exc:
        entry["problem"] = f"Damaged inside the ZIP ({type(exc).__name__})."
        (root / "files" / entry["id"]).unlink(missing_ok=True)
        return
    entry["protected"] = protection.detect(root / "files" / entry["id"], entry["name"])


def _nested(zf: zipfile.ZipFile, info: zipfile.ZipInfo, prefix: str, depth: int, root: Path, limits: _Limits,
            entries: list[dict[str, Any]]) -> bool:
    """Open a ZIP found inside the ZIP, listing its files under its own name. False: it is not a ZIP after all (or too deep),
    and the caller stores it like any other file."""
    if depth >= MAX_DEPTH:
        return False
    tmp = root / f"nested-{uuid.uuid4().hex}.zip"
    try:
        with zf.open(info) as src, tmp.open("wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        if not zipfile.is_zipfile(tmp):
            return False
        with zipfile.ZipFile(tmp) as inner:
            _walk(inner, prefix, depth + 1, root, limits, entries)
        return True
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError):
        return False
    finally:
        tmp.unlink(missing_ok=True)
