"""Unit D -- a processed zip leaves processing/ for processed/ (never lingers)."""

import json
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_archive as fma  # noqa: E402

EXTS = (".mov", ".MOV")


def _mkzip(dirpath, name, entries):
    """entries: list of (arcname, bytes)."""
    path = os.path.join(str(dirpath), name)
    with zipfile.ZipFile(path, "w") as zf:
        for arc, data in entries:
            zf.writestr(arc, data)
    return path


def _record(zip_path, basenames):
    state = {"zip": os.path.basename(zip_path), "entries": {}}
    for bn in basenames:
        state["entries"][bn] = {"exists": True}
    with open(zip_path + ".archive.json", "w") as fh:
        json.dump(state, fh)


def test_zip_is_complete_true_when_all_entries_recorded(tmp_path):
    z = _mkzip(tmp_path, "a.zip", [("x.mp4.mov", b"1"), ("y.MOV", b"2")])
    _record(z, ["x.mp4.mov", "y.MOV"])
    assert fma.zip_is_complete(z, EXTS) is True


def test_zip_is_complete_false_when_an_entry_missing(tmp_path):
    z = _mkzip(tmp_path, "a.zip", [("x.mov", b"1"), ("y.mov", b"2")])
    _record(z, ["x.mov"])
    assert fma.zip_is_complete(z, EXTS) is False


def test_zip_is_complete_false_for_no_media_entries(tmp_path):
    z = _mkzip(tmp_path, "a.zip", [("notes.txt", b"hi")])
    assert fma.zip_is_complete(z, EXTS) is False


def test_promote_moves_zip_and_sidecar(tmp_path):
    z = _mkzip(tmp_path, "done.zip", [("x.mov", b"1")])
    _record(z, ["x.mov"])
    dst = fma.promote_zip(z, str(tmp_path / "processed"))
    assert os.path.basename(dst) == "done.zip"
    assert os.path.exists(tmp_path / "processed" / "done.zip")
    assert os.path.exists(tmp_path / "processed" / "done.zip.archive.json")
    assert not os.path.exists(z)  # left processing


def test_promote_never_overwrites(tmp_path):
    z = _mkzip(tmp_path, "clash.zip", [("x.mov", b"1")])
    proc = tmp_path / "processed"
    proc.mkdir()
    (proc / "clash.zip").write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        fma.promote_zip(z, str(proc))


def test_process_zip_dir_archives_then_promotes(tmp_path, monkeypatch):
    src = tmp_path / "processing"
    src.mkdir()
    z = _mkzip(src, "farm.zip", [("clip.mov", b"1")])
    os.utime(z, (0, 0))

    def fake_handle(s3, bucket, farm_id, zip_path, exts, frac):
        _record(zip_path, ["clip.mov"])  # simulate archive success
        return True

    monkeypatch.setattr(fma, "handle_zip_root", fake_handle)
    made = fma.process_zip_dir(
        None, "b", "f", str(src), str(tmp_path / "processed"), EXTS, 0.25, 0
    )
    assert made is True
    assert os.path.exists(tmp_path / "processed" / "farm.zip")
    assert not os.path.exists(z)


def test_process_zip_dir_leaves_incomplete_zip(tmp_path, monkeypatch):
    src = tmp_path / "processing"
    src.mkdir()
    z = _mkzip(src, "farm.zip", [("clip.mov", b"1")])
    os.utime(z, (0, 0))
    monkeypatch.setattr(fma, "handle_zip_root", lambda *a, **k: False)
    fma.process_zip_dir(
        None, "b", "f", str(src), str(tmp_path / "processed"), EXTS, 0.25, 0
    )
    assert os.path.exists(z)  # stays put
    assert not (tmp_path / "processed" / "farm.zip").exists()


def test_process_zip_dir_settle_guard(tmp_path, monkeypatch):
    src = tmp_path / "processing"
    src.mkdir()
    _mkzip(src, "fresh.zip", [("clip.mov", b"1")])  # mtime = now
    called = []
    monkeypatch.setattr(
        fma, "handle_zip_root", lambda *a, **k: called.append(1) or True
    )
    fma.process_zip_dir(
        None, "b", "f", str(src), str(tmp_path / "processed"), EXTS, 0.25, 300
    )
    assert called == []  # untouched


def test_process_zip_dir_missing_dir_is_noop(tmp_path):
    assert (
        fma.process_zip_dir(
            None,
            "b",
            "f",
            str(tmp_path / "nope"),
            str(tmp_path / "processed"),
            EXTS,
            0.25,
            0,
        )
        is False
    )


def test_process_zip_dir_uses_per_zip_farm_id(tmp_path, monkeypatch):
    """A shared intake dir maps each zip to its OWN farm_id/namespace."""
    src = tmp_path / "processing"
    src.mkdir()
    z = _mkzip(src, "sao_jorge.zip", [("clip.mov", b"1")])
    os.utime(z, (0, 0))
    seen = {}

    def fake_handle(s3, bucket, farm_id, zip_path, exts, frac):
        seen["farm_id"] = farm_id
        _record(zip_path, ["clip.mov"])
        return True

    monkeypatch.setattr(fma, "handle_zip_root", fake_handle)
    fma.process_zip_dir(
        None,
        "b",
        "intake",
        str(src),
        str(tmp_path / "processed"),
        EXTS,
        0.25,
        0,
        {"sao_jorge.zip": "fazenda-sao-jorge-bahia"},
    )
    assert seen["farm_id"] == "fazenda-sao-jorge-bahia"
    assert os.path.exists(tmp_path / "processed" / "sao_jorge.zip")


def test_process_zip_dir_skips_unmapped_zip_when_map_given(tmp_path, monkeypatch):
    """A zip absent from zip_farm_ids is NOT archived (never mis-filed)."""
    src = tmp_path / "processing"
    src.mkdir()
    z = _mkzip(src, "mystery.zip", [("clip.mov", b"1")])
    os.utime(z, (0, 0))
    called = []
    monkeypatch.setattr(
        fma, "handle_zip_root", lambda *a, **k: called.append(1) or True
    )
    made = fma.process_zip_dir(
        None,
        "b",
        "placeholder",
        str(src),
        str(tmp_path / "processed"),
        EXTS,
        0.25,
        0,
        {"other.zip": "some-farm"},
    )
    assert called == []  # never archived
    assert os.path.exists(z)  # left in place for a human to map
    assert made is False
