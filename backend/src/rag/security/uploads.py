"""Upload validation: size, type by content (not by name), container limits, file name hygiene, optional ClamAV."""

from __future__ import annotations

import io
import re
import socket
import struct
import unicodedata
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlparse

from rag.errors import PermanentError
from rag.ingestion.formats import SUPPORTED, detect_format, extension

MAX_OOXML_MEMBERS = 10_000
MAX_OOXML_EXPANDED = 2 * 1024**3
MAX_OOXML_RATIO = 250


class UploadRejected(PermanentError):
    pass


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", PurePosixPath((name or "").replace("\\", "/")).name)
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", name).strip(" .")
    if not name:
        name = "upload"
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:170] + dot + ext[:8]) if dot else name[:180]
    return name


def validate_upload(data: bytes, filename: str, *, max_bytes: int) -> str:
    """Return the detected format or raise UploadRejected with a message safe to show the user."""
    if not data:
        raise UploadRejected("The file is empty.")
    if len(data) > max_bytes:
        raise UploadRejected(f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
    ext = extension(filename)
    if ext and ext not in SUPPORTED:
        raise UploadRejected(f".{ext} files are not supported.")
    try:
        fmt = detect_format(data, filename)
    except PermanentError as exc:
        raise UploadRejected(str(exc)) from exc
    if data[:4] == b"PK\x03\x04":
        _check_zip(data)
    return fmt


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise UploadRejected("The file is a damaged zip/Office container.") from exc
    if len(infos) > MAX_OOXML_MEMBERS:
        raise UploadRejected("The file contains too many parts.")
    expanded = sum(i.file_size for i in infos)
    if expanded > MAX_OOXML_EXPANDED or expanded > max(len(data), 1) * MAX_OOXML_RATIO:
        raise UploadRejected("The file expands to an unsafe size (possible zip bomb).")
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts:
            raise UploadRejected("The file contains unsafe paths.")


def clamav_scan(data: bytes, url: str, *, timeout_s: float = 30.0) -> None:
    """Scan with clamd's INSTREAM command (tcp://host:3310). Raises UploadRejected when a virus is found."""
    parsed = urlparse(url)
    with socket.create_connection((parsed.hostname or "localhost", parsed.port or 3310), timeout=timeout_s) as sock:
        sock.sendall(b"zINSTREAM\0")
        view = memoryview(data)
        for start in range(0, len(view), 1 << 20):
            chunk = view[start: start + (1 << 20)]
            sock.sendall(struct.pack("!L", len(chunk)) + chunk)
        sock.sendall(struct.pack("!L", 0))
        reply = sock.recv(4096).decode(errors="replace").strip("\0 \n")
    if "FOUND" in reply:
        raise UploadRejected("The file was rejected by the virus scanner.")
    if not reply.endswith("OK"):
        raise PermanentError(f"virus scanner error: {reply[:200]}")
