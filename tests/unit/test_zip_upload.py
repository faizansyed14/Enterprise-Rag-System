"""A ZIP folder is opened first (nested ZIPs too), listed with default ticks, and only the ticked files are uploaded."""
import asyncio
import io
import time
import zipfile
from types import SimpleNamespace

import pytest

from rag.api.routers import uploads
from rag.errors import PermanentError
from rag.ingestion import archive


def _settings(tmp_path, **over):
    base = dict(upload_staging_dir=str(tmp_path), cache_dir=str(tmp_path), upload_max_mb=1, upload_zip_max_mb=10,
                skip_extension_set=frozenset({"jpg", "jpeg", "png"}))
    base.update(over)
    return SimpleNamespace(**base)


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _tree() -> bytes:
    inner = _zip({"archive/old/minutes.txt": b"2019 minutes", "archive/old/photo.jpg": b"\xff\xd8\xff"})
    return _zip({
        "Project A/readme.txt": b"top level",
        "Project A/Contracts/2024/Q1/supplier.txt": b"agreement",
        "Project A/Drawings/level1.dwg": b"AC1027",
        "Project A/Drawings/Details/site.JPG": b"\xff\xd8\xff",
        "Project A/Photos/shot.png": b"\x89PNG",
        "Project A/data/prices.csv": b"item,price\nbolt,12.5\n",
        "Project A/tool.exe": b"MZ",
        "Project A/nested.zip": inner,
        "__MACOSX/Project A/._readme.txt": b"junk",
        "Thumbs.db": b"junk",
    })


def _extract(settings, data: bytes, owner: str = "u1", name: str = "Site.zip"):
    return asyncio.run(uploads.extract_zip(SimpleNamespace(id=owner), SimpleNamespace(settings=settings),
                                           SimpleNamespace(file=io.BytesIO(data), filename=name)))


def test_every_folder_and_nested_zip_is_listed_with_its_path(tmp_path):
    out = _extract(_settings(tmp_path), _tree())
    paths = {f["path"] for f in out["files"]}
    assert paths == {
        "Project A/readme.txt", "Project A/Contracts/2024/Q1/supplier.txt", "Project A/Drawings/level1.dwg",
        "Project A/Drawings/Details/site.JPG", "Project A/Photos/shot.png", "Project A/data/prices.csv",
        "Project A/tool.exe", "Project A/nested.zip/archive/old/minutes.txt", "Project A/nested.zip/archive/old/photo.jpg",
    }  # operating-system files are not listed; the nested ZIP is a folder
    assert len(out["files"]) == 9


def test_images_are_listed_but_unticked_while_documents_and_drawings_are_ticked(tmp_path):
    files = {f["path"]: f for f in _extract(_settings(tmp_path), _tree())["files"]}
    for ticked in ("Project A/readme.txt", "Project A/data/prices.csv", "Project A/nested.zip/archive/old/minutes.txt"):
        assert files[ticked]["selected"] and files[ticked]["supported"], ticked
    for unticked in ("Project A/Drawings/Details/site.JPG", "Project A/Photos/shot.png",
                     "Project A/nested.zip/archive/old/photo.jpg"):
        assert not files[unticked]["selected"] and files[unticked]["supported"], unticked  # can be ticked by hand
    drawing = files["Project A/Drawings/level1.dwg"]
    assert drawing["supported"] and drawing["selected"]  # AutoCAD drawings are read (rag.ingestion.cad)
    assert not files["Project A/tool.exe"]["selected"] and not files["Project A/tool.exe"]["supported"]


def test_removing_a_type_from_the_default_list_ticks_it(tmp_path):
    out = _extract(_settings(tmp_path, skip_extension_set=frozenset()), _tree())
    assert {f["ext"] for f in out["files"] if f["selected"]} >= {"jpg", "png", "txt", "csv"}


def test_oversized_members_are_listed_with_the_reason(tmp_path):
    out = _extract(_settings(tmp_path), _zip({"big.txt": b"x" * (2 * 1024 * 1024), "ok.txt": b"fine"}))
    files = {f["path"]: f for f in out["files"]}
    assert files["ok.txt"]["selected"] and not files["ok.txt"]["problem"]
    assert "Larger than 1 MB" in files["big.txt"]["problem"] and not files["big.txt"]["selected"]


def test_a_zip_over_the_limit_or_damaged_is_refused(tmp_path):
    with pytest.raises(Exception) as big:
        _extract(_settings(tmp_path, upload_zip_max_mb=0), _zip({"a.txt": b"1"}))
    assert "UPLOAD_ZIP_MAX_MB" in str(big.value.detail)
    with pytest.raises(Exception) as bad:
        _extract(_settings(tmp_path), b"PK\x03\x04 not really a zip")
    assert "damaged" in str(bad.value.detail)


def test_only_the_owner_can_use_a_staged_upload_and_it_can_be_discarded(tmp_path):
    settings = _settings(tmp_path)
    out = _extract(settings, _tree(), owner="u1")
    with pytest.raises(PermanentError):
        archive.load(settings, out["staging_id"], "someone-else")
    archive.load(settings, out["staging_id"], "u1")
    asyncio.run(uploads.discard_zip(out["staging_id"], SimpleNamespace(id="u1"), SimpleNamespace(settings=settings)))
    with pytest.raises(PermanentError):
        archive.load(settings, out["staging_id"], "u1")


def test_forgotten_staging_folders_are_swept_after_their_time(tmp_path):
    settings = _settings(tmp_path)
    out = _extract(settings, _tree())
    assert archive.sweep(settings, max_age_s=3600) == 0
    time.sleep(0.05)
    assert archive.sweep(settings, max_age_s=0.01) == 1
    with pytest.raises(PermanentError):
        archive.load(settings, out["staging_id"], "u1")


def test_commit_uploads_only_the_selected_files_and_the_rest_can_follow_until_discarded(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    out = _extract(settings, _tree())
    by_path = {f["path"]: f["id"] for f in out["files"]}
    registered = []

    async def fake_register(target, name, rel, data, mime, archive=None):
        registered.append((rel, data, archive))
        return {"file": rel, "archive": archive, "ok": True, "item_id": "x"}

    async def fake_target(*args, **kwargs):
        return SimpleNamespace()

    async def noop_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(uploads, "_register", fake_register)
    monkeypatch.setattr(uploads, "_target", fake_target)
    monkeypatch.setattr(uploads, "audit", noop_audit)
    chosen = [by_path["Project A/readme.txt"], by_path["Project A/Photos/shot.png"],  # a photo ticked by hand
              by_path["Project A/nested.zip/archive/old/minutes.txt"], "f999"]
    body = uploads.CommitBody(staging_id=out["staging_id"], ids=chosen)
    result = asyncio.run(uploads.commit_zip(body, SimpleNamespace(client=None), SimpleNamespace(id="u1"), [],
                                            SimpleNamespace(settings=settings)))
    assert sorted(r for r, _, _ in registered) == [
        "Site.zip/Project A/Photos/shot.png", "Site.zip/Project A/nested.zip/archive/old/minutes.txt",
        "Site.zip/Project A/readme.txt"]
    assert dict((r, d) for r, d, _ in registered)["Site.zip/Project A/readme.txt"] == b"top level"
    assert (result["queued"], result["failed"]) == (3, 1)  # the unknown id is reported, not silently dropped
    archive.load(settings, out["staging_id"], "u1")  # still there: more files can be ticked and uploaded
    later = uploads.CommitBody(staging_id=out["staging_id"], ids=[by_path["Project A/data/prices.csv"]])
    again = asyncio.run(uploads.commit_zip(later, SimpleNamespace(client=None), SimpleNamespace(id="u1"), [],
                                           SimpleNamespace(settings=settings)))
    assert again["queued"] == 1
    asyncio.run(uploads.discard_zip(out["staging_id"], SimpleNamespace(id="u1"), SimpleNamespace(settings=settings)))
    with pytest.raises(PermanentError):
        archive.load(settings, out["staging_id"], "u1")
