"""Unit A -- the intake front door must claim safely and never lose a zip."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_intake as fma  # noqa: E402

GB = 1024**3


def _mkzip(dirpath, name, payload=b"data"):
    p = os.path.join(str(dirpath), name)
    with open(p, "wb") as fh:
        fh.write(payload)
    os.utime(p, (0, 0))  # settled long ago
    return p


def _cfg(tmp_path, **over):
    inc = {
        "to_process": str(tmp_path / "to_process"),
        "processing": str(tmp_path / "processing"),
        "duplicates": str(tmp_path / "duplicates"),
        "ledger": str(tmp_path / "ledger.json"),
        "settle_seconds": 300,
        "min_free_gb": 0,
    }
    inc.update(over)
    os.makedirs(inc["to_process"], exist_ok=True)
    return {"intake": inc}


def test_claims_new_zip_and_records_ledger(tmp_path):
    cfg = _cfg(tmp_path)
    _mkzip(cfg["intake"]["to_process"], "farmA.zip")
    res = fma.run_intake(cfg, free_bytes_fn=lambda p: 100 * GB)
    assert res["claimed"] == ["farmA.zip"]
    assert os.path.exists(os.path.join(cfg["intake"]["processing"], "farmA.zip"))
    assert not os.listdir(cfg["intake"]["to_process"])
    import json

    with open(cfg["intake"]["ledger"]) as fh:
        ledger = json.load(fh)
    assert len(ledger["claimed"]) == 1


def test_settle_guard_skips_fresh_zip(tmp_path):
    cfg = _cfg(tmp_path)
    fresh = _mkzip(cfg["intake"]["to_process"], "still_uploading.zip")
    os.utime(fresh, None)  # mtime = now
    res = fma.run_intake(cfg, free_bytes_fn=lambda p: 100 * GB)
    assert res["claimed"] == []
    assert os.path.exists(fresh)  # untouched


def test_duplicate_is_moved_aside_not_reprocessed(tmp_path):
    cfg = _cfg(tmp_path)
    _mkzip(cfg["intake"]["to_process"], "first.zip", b"same-bytes")
    fma.run_intake(cfg, free_bytes_fn=lambda p: 100 * GB)
    # a second, identical drop arrives
    _mkzip(cfg["intake"]["to_process"], "second.zip", b"same-bytes")
    res = fma.run_intake(cfg, free_bytes_fn=lambda p: 100 * GB)
    assert res["claimed"] == []
    assert res["duplicate"] == ["second.zip"]
    assert os.path.exists(os.path.join(cfg["intake"]["duplicates"], "second.zip"))


def test_disk_guard_refuses_and_reports(tmp_path):
    cfg = _cfg(tmp_path, min_free_gb=20)
    _mkzip(cfg["intake"]["to_process"], "big.zip")
    res = fma.run_intake(cfg, free_bytes_fn=lambda p: 1 * GB)  # below headroom
    assert res["refused"] == "big.zip"
    assert res["claimed"] == []
    # the zip is left IN PLACE for a future run
    assert os.path.exists(os.path.join(cfg["intake"]["to_process"], "big.zip"))


def test_dry_run_changes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    _mkzip(cfg["intake"]["to_process"], "farmB.zip")
    res = fma.run_intake(cfg, dry_run=True, free_bytes_fn=lambda p: 100 * GB)
    assert res["claimed"] == ["farmB.zip"]  # reported ...
    assert os.path.exists(os.path.join(cfg["intake"]["to_process"], "farmB.zip"))
    assert not os.path.exists(cfg["intake"]["ledger"])  # ... but nothing written
    assert not os.path.isdir(cfg["intake"]["processing"])


def test_never_overwrites_existing_target(tmp_path):
    cfg = _cfg(tmp_path)
    _mkzip(cfg["intake"]["to_process"], "clash.zip", b"new")
    os.makedirs(cfg["intake"]["processing"], exist_ok=True)
    with open(os.path.join(cfg["intake"]["processing"], "clash.zip"), "wb") as fh:
        fh.write(b"already here")
    with pytest.raises(FileExistsError):
        fma.run_intake(cfg, free_bytes_fn=lambda p: 100 * GB)


def test_cross_filesystem_move_is_refused(tmp_path, monkeypatch):
    src = tmp_path / "a"
    dst = tmp_path / "b"
    src.mkdir()
    dst.mkdir()
    real_stat = os.stat

    def fake_stat(p, *a, **k):
        st = real_stat(p, *a, **k)
        if str(p) == str(dst):
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    st.st_dev + 1,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    st.st_size,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )
        return st

    monkeypatch.setattr(fma.os, "stat", fake_stat)
    with pytest.raises(OSError):
        fma._require_same_fs(str(src), str(dst), "z.zip")


def test_free_bytes_walks_up_missing_dir(tmp_path):
    """The disk guard must work even when processing/ does not exist yet."""
    missing = tmp_path / "nope" / "processing"
    assert fma._free_bytes(str(missing)) > 0


def test_dry_run_on_missing_processing_dir(tmp_path):
    """The exact real-world dry-run: no processing/ dir, ledger absent."""
    cfg = _cfg(tmp_path)  # processing/ deliberately not created
    _mkzip(cfg["intake"]["to_process"], "farmC.zip")
    res = fma.run_intake(cfg, dry_run=True)  # real _free_bytes, no injected fn
    assert res["claimed"] == ["farmC.zip"]


def test_junk_and_partial_files_ignored(tmp_path):
    cfg = _cfg(tmp_path)
    tp = cfg["intake"]["to_process"]
    for name in [".DS_Store", "._farm.zip", "farm.zip.part", "notes.txt"]:
        _mkzip(tp, name)
    res = fma.run_intake(cfg, free_bytes_fn=lambda p: 100 * GB)
    assert res["claimed"] == []
