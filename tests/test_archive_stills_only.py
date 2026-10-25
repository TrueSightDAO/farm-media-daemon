"""Regression: a stills-only zip must not dead-drop in the zip_dir.

Bug (2026-10-03..2026-10-25): ``ilheus_warehouse_2.zip`` (2 HEIC stills, 0
videos) sat in /media/processing forever. ``zip_is_complete`` is keyed on the
S3-archivable (video) extensions, and a stills-only zip has none of those, so it
could never report complete and was never promoted to processed_dir.

Contract under test: stills are NEVER archived to S3, but a stills-only drop is
recorded and PROMOTED.
"""

import json
import os
import zipfile

import farm_media_archive as fma

VIDEO = (".MOV", ".mov", ".mp4")


def _make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_zip_still_entries_finds_heic_and_skips_junk(tmp_path):
    z = _make_zip(
        str(tmp_path / "s.zip"),
        {
            "IMG_0128.HEIC": b"x",
            "IMG_9922.heic": b"x",
            "__MACOSX/._IMG_0128.HEIC": b"junk",
            "IMG_1.MOV": b"video",
        },
    )
    assert sorted(fma.zip_still_entries(z)) == ["IMG_0128.HEIC", "IMG_9922.heic"]


def test_stills_only_zip_is_detected(tmp_path):
    z = _make_zip(str(tmp_path / "s.zip"), {"A.HEIC": b"x", "B.JPG": b"x"})
    assert fma.zip_is_stills_only(z, VIDEO) is True
    # nothing archivable -> the old completeness check is (correctly) False
    assert fma.zip_is_complete(z, VIDEO) is False


def test_zip_with_video_is_not_treated_as_stills_only(tmp_path):
    z = _make_zip(str(tmp_path / "s.zip"), {"A.HEIC": b"x", "B.MOV": b"v"})
    assert fma.zip_is_stills_only(z, VIDEO) is False


def test_empty_or_bad_zip_is_not_stills_only(tmp_path):
    z = _make_zip(str(tmp_path / "s.zip"), {"notes.txt": b"hi"})
    assert fma.zip_is_stills_only(z, VIDEO) is False
    bad = str(tmp_path / "bad.zip")
    open(bad, "wb").write(b"not a zip")
    assert fma.zip_is_stills_only(bad, VIDEO) is False


def test_record_stills_only_stamps_sidecar(tmp_path):
    z = _make_zip(str(tmp_path / "s.zip"), {"IMG_0128.HEIC": b"x"})
    fma.record_stills_only(z)
    side = json.load(open(z + ".archive.json", encoding="utf-8"))
    assert side["skipped"] == "stills_only"
    assert side["stills"] == ["IMG_0128.HEIC"]
    assert "farm-media-raw" in side["skip_reason"]


def test_process_zip_dir_promotes_stills_only_zip(tmp_path):
    """The actual dead-drop: a stills-only zip must end up in processed_dir."""
    zip_dir = tmp_path / "processing"
    processed = tmp_path / "processed"
    zip_dir.mkdir()
    z = _make_zip(
        str(zip_dir / "stills.zip"), {"IMG_0128.HEIC": b"x", "IMG_9922.HEIC": b"y"}
    )
    (zip_dir / "stills.zip.context.json").write_text(
        json.dumps({"farm_id": "facility-black-king-warehouse"}), encoding="utf-8"
    )
    # settle_seconds=0 so the fresh mtime is not skipped
    fma.process_zip_dir(
        s3=None,
        bucket="b",
        farm_id="intake",
        zip_dir=str(zip_dir),
        processed_dir=str(processed),
        exts=VIDEO,
        frac=0.25,
        settle_seconds=0,
        zip_farm_ids={"stills.zip": "facility-black-king-warehouse"},
    )
    assert not os.path.exists(z), "zip should have been promoted out of processing"
    assert (processed / "stills.zip").exists()
    assert (processed / "stills.zip.archive.json").exists()
    side = json.load(open(processed / "stills.zip.archive.json", encoding="utf-8"))
    assert side["skipped"] == "stills_only"
    assert side["stills"] == ["IMG_0128.HEIC", "IMG_9922.HEIC"]
