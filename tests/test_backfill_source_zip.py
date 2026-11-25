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


def test_alias_maps_inbox_farm_to_archive_farm(tmp_path):
    """The inbox dir is "<delivery>-para"; the archive sidecar says the canonical farm."""
    proc, inbox = str(tmp_path / "p"), str(tmp_path / "i")
    _archive(
        proc,
        "raimundo.zip",
        {"IMG_7.MOV": {"farm_id": "raimundo-geniza", "sha256": "bb"}},
    )
    _inbox(
        proc,
        inbox,
        "raimundo-geniza-para",
        "IMG_7.mp4.json",
        {"file": "IMG_7.mp4", "sha256": "bb"},
    )
    st = B.backfill(inbox, proc, apply=True)
    assert st["stamped"] == 1
    assert st["alias_resolved"] == 1
    with open(
        os.path.join(inbox, "farm-media", "raimundo-geniza-para", "IMG_7.mp4.json")
    ) as fh:
        assert json.load(fh)["source_zip"] == "raimundo.zip"


def test_la_do_sitio_is_never_aliased(tmp_path):
    """la-do-sitio and paulo-la-do-sitio are DISTINCT farms -- must not conflate."""
    assert "la-do-sitio" not in B.FARM_ALIASES
    assert B.resolve_farm_ids("la-do-sitio") == ["la-do-sitio"]
    assert "la-do-sitio" in B.UNALIASED_BY_DESIGN

    proc, inbox = str(tmp_path / "p"), str(tmp_path / "i")
    # archive data belongs to the OTHER farm; a naive alias would mis-file onto it
    _archive(
        proc,
        "paulo.zip",
        {"IMG_5.MOV": {"farm_id": "paulo-la-do-sitio", "sha256": "cc"}},
    )
    _inbox(
        proc,
        inbox,
        "la-do-sitio",
        "IMG_5.mp4.json",
        {"file": "IMG_5.mp4", "sha256": "cc"},
    )
    st = B.backfill(inbox, proc, apply=True)
    assert st["stamped"] == 0 and st["unmatched"] == 1
    with open(os.path.join(inbox, "farm-media", "la-do-sitio", "IMG_5.mp4.json")) as fh:
        assert "source_zip" not in json.load(fh)


def test_index_reads_multiple_roots(tmp_path):
    """Legacy strays outside /media/processed still resolve."""
    root1, root2 = str(tmp_path / "p1"), str(tmp_path / "p2")
    inbox = str(tmp_path / "i")
    _archive(root2, "old.zip", {"IMG_3.MOV": {"farm_id": "faz", "sha256": "dd"}})
    _inbox(
        proc=None,
        inbox=inbox,
        farm="faz",
        name="IMG_3.mp4.json",
        side={"file": "IMG_3.mp4", "sha256": "dd"},
    )
    st = B.backfill(inbox, (root1, root2), apply=True)
    assert st["stamped"] == 1
    with open(os.path.join(inbox, "farm-media", "faz", "IMG_3.mp4.json")) as fh:
        assert json.load(fh)["source_zip"] == "old.zip"
