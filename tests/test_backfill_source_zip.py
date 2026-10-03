"""Tests for farm_media_backfill_source_zip (governor thread 30550)."""

import json
import os

import farm_media_backfill_source_zip as B


def _w(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(d, fh)


def _archive(proc, zip_name, entries):
    _w(
        os.path.join(proc, zip_name + ".archive.json"),
        {"zip": zip_name, "entries": entries},
    )


def _inbox(proc, inbox, farm, name, side):
    d = os.path.join(inbox, "farm-media", farm)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        json.dump(side, fh)


def test_stamps_by_sha_and_by_stem(tmp_path):
    proc, inbox = str(tmp_path / "p"), str(tmp_path / "i")
    # primary join: same sha256, transcoded .mp4 vs original .MOV
    _archive(proc, "faz.zip", {"IMG_1.MOV": {"farm_id": "faz", "sha256": "aa"}})
    _inbox(proc, inbox, "faz", "IMG_1.mp4.json", {"file": "IMG_1.mp4", "sha256": "aa"})
    _inbox(proc, inbox, "faz", "IMG_2.mp4.json", {"file": "IMG_2.mp4"})
    _archive(
        proc,
        "faz2.zip",
        {"IMG_2.MOV": {"exists": True, "raw_url": "https://x/raw/faz/IMG_2.MOV"}},
    )

    st = B.backfill(inbox, proc, apply=True)
    assert st["stamped"] == 2
    with open(os.path.join(inbox, "farm-media", "faz", "IMG_1.mp4.json")) as fh:
        got = json.load(fh)
    assert got["source_zip"] == "faz.zip"
    with open(os.path.join(inbox, "farm-media", "faz", "IMG_2.mp4.json")) as fh:
        got2 = json.load(fh)
    assert got2["source_zip"] == "faz2.zip"


def test_never_overwrites_existing_and_idempotent(tmp_path):
    proc, inbox = str(tmp_path / "p"), str(tmp_path / "i")
    _archive(proc, "faz.zip", {"IMG_1.MOV": {"farm_id": "faz", "sha256": "aa"}})
    _inbox(
        proc,
        inbox,
        "faz",
        "IMG_1.mp4.json",
        {"file": "IMG_1.mp4", "sha256": "aa", "source_zip": "ORIGINAL.zip"},
    )
    st = B.backfill(inbox, proc, apply=True)
    assert st["already"] == 1 and st["stamped"] == 0
    with open(os.path.join(inbox, "farm-media", "faz", "IMG_1.mp4.json")) as fh:
        got = json.load(fh)
    assert got["source_zip"] == "ORIGINAL.zip"


def test_ambiguous_stem_is_skipped_not_guessed(tmp_path):
    proc, inbox = str(tmp_path / "p"), str(tmp_path / "i")
    # same farm+stem in two different zips, and the inbox sidecar has no sha
    _archive(proc, "a.zip", {"IMG_9.MOV": {"farm_id": "faz"}})
    _archive(proc, "b.zip", {"IMG_9.MOV": {"farm_id": "faz"}})
    _inbox(proc, inbox, "faz", "IMG_9.mp4.json", {"file": "IMG_9.mp4"})
    st = B.backfill(inbox, proc, apply=True)
    assert st["ambiguous_skipped"] == 1 and st["stamped"] == 0
    with open(os.path.join(inbox, "farm-media", "faz", "IMG_9.mp4.json")) as fh:
        got = json.load(fh)
    assert "source_zip" not in got


def test_dry_run_writes_nothing(tmp_path):
    proc, inbox = str(tmp_path / "p"), str(tmp_path / "i")
    _archive(proc, "faz.zip", {"IMG_1.MOV": {"farm_id": "faz", "sha256": "aa"}})
    _inbox(proc, inbox, "faz", "IMG_1.mp4.json", {"file": "IMG_1.mp4", "sha256": "aa"})
    st = B.backfill(inbox, proc, apply=False)
    assert st["stamped"] == 1
    with open(os.path.join(inbox, "farm-media", "faz", "IMG_1.mp4.json")) as fh:
        got = json.load(fh)
    assert "source_zip" not in got
