"""Per-zip context card (thread 30550 \u00a74.1): intake ferries it, archive honours it.

Covered:
  - intake: card travels to_process -> processing; card present -> not awaiting;
    absent -> awaiting + ledger stamped; malformed -> treated as absent AND flagged;
    card for a different zip never bleeds across.
  - archive: card farm_id WINS over zip_farm_ids; card present lets an unmapped zip
    through; no card + unmapped -> held (fail-closed); card ferried on promote.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_archive as farch
import farm_media_intake as fma

GB = 1024**3


def _mksettled(dirpath, name, payload=None):
    payload = (name.encode() or b"zip") if payload is None else payload
    p = os.path.join(str(dirpath), name)
    with open(p, "wb") as fh:
        fh.write(payload)
    os.utime(p, (0, 0))
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
    return {"intake": inc}


def _prep(tmp_path, cfg):
    for k in ("to_process", "processing", "duplicates"):
        os.makedirs(cfg["intake"][k], exist_ok=True)


# --------------------------- intake ---------------------------


def test_card_travels_with_zip_and_marks_claimed(tmp_path):
    cfg = _cfg(tmp_path)
    _prep(tmp_path, cfg)
    z = _mksettled(cfg["intake"]["to_process"], "farm_a.zip")
    with open(z + ".context.json", "w") as fh:
        json.dump({"farm_id": "fazenda-a", "title": "Farm A"}, fh)

    res = fma.run_intake(cfg, now=1e9)

    assert res["claimed"] == ["farm_a.zip"]
    assert res["awaiting_context"] == []
    # zip AND card have both landed in processing/
    assert os.path.exists(cfg["intake"]["processing"] + "/farm_a.zip")
    assert os.path.exists(cfg["intake"]["processing"] + "/farm_a.zip.context.json")
    assert not os.path.exists(z + ".context.json")  # left to_process


def test_zip_without_card_is_awaiting_context(tmp_path):
    cfg = _cfg(tmp_path)
    _prep(tmp_path, cfg)
    _mksettled(cfg["intake"]["to_process"], "orphan.zip")

    res = fma.run_intake(cfg, now=1e9)

    assert res["claimed"] == ["orphan.zip"]
    assert res["awaiting_context"] == ["orphan.zip"]
    with open(cfg["intake"]["ledger"]) as fh:
        ledger = json.load(fh)
    entry = next(iter(ledger["claimed"].values()))
    assert entry["awaiting_context"] is True


def test_malformed_card_is_flagged_not_silent(tmp_path):
    cfg = _cfg(tmp_path)
    _prep(tmp_path, cfg)
    z = _mksettled(cfg["intake"]["to_process"], "broken.zip")
    with open(z + ".context.json", "w") as fh:
        fh.write("{not: json")

    res = fma.run_intake(cfg, now=1e9)

    assert res["claimed"] == ["broken.zip"]  # never hidden
    assert any("bad card" in x for x in res["awaiting_context"])
    with open(cfg["intake"]["ledger"]) as fh:
        ledger = json.load(fh)
    assert "context_error" in next(iter(ledger["claimed"].values()))


def test_card_for_a_different_zip_does_not_bleed(tmp_path):
    cfg = _cfg(tmp_path)
    _prep(tmp_path, cfg)
    _mksettled(cfg["intake"]["to_process"], "wanted.zip")
    other = _mksettled(cfg["intake"]["to_process"], "other.zip")
    with open(other + ".context.json", "w") as fh:
        json.dump({"farm_id": "farm-other"}, fh)

    res = fma.run_intake(cfg, now=1e9)

    # wanted.zip has no card of its own, so it is held
    assert "wanted.zip" in res["awaiting_context"]
    assert "other.zip" not in res["awaiting_context"]  # its own card travelled


def test_awaiting_context_digest_lists_unmapped(tmp_path):
    cfg = _cfg(tmp_path)
    _prep(tmp_path, cfg)
    _mksettled(cfg["intake"]["processing"], "held.zip")
    listed = fma.awaiting_context_zips(cfg["intake"]["processing"], cfg)
    assert listed == ["held.zip"]
    # but a configured zip_farm_ids entry clears it
    cfg2 = {
        "intake": cfg["intake"],
        "archive": {"roots": [{"zip_farm_ids": {"held.zip": "some-farm"}}]},
    }
    assert fma.awaiting_context_zips(cfg["intake"]["processing"], cfg2) == []


# --------------------------- archive ---------------------------


def test_card_farm_id_wins_over_zip_farm_ids(tmp_path):
    zdir = tmp_path / "proc"
    zdir.mkdir()
    z = _mksettled(zdir, "farm_a.zip")
    with open(z + ".context.json", "w") as fh:
        json.dump({"farm_id": "from-card"}, fh)
    seen = {}

    def fake_handle(s3, bucket, farm_id, zip_path, exts, frac):
        seen["farm_id"] = farm_id
        with open(zip_path + ".archive.json", "w") as fh:
            json.dump({"zip": os.path.basename(zip_path), "entries": {"x.mov": {}}}, fh)
        return True

    orig = farch.handle_zip_root
    farch.handle_zip_root = fake_handle
    try:
        farch.process_zip_dir(
            None,
            "b",
            "root-farm",
            str(zdir),
            str(tmp_path / "processed"),
            (".mov",),
            0.25,
            0,
            zip_farm_ids={"farm_a.zip": "from-map"},
        )
    finally:
        farch.handle_zip_root = orig
    assert seen["farm_id"] == "from-card"


def test_unmapped_zip_without_card_is_held(tmp_path):
    zdir = tmp_path / "proc"
    zdir.mkdir()
    _mksettled(zdir, "mystery.zip")
    called = {"n": 0}

    def fake_handle(*a, **k):
        called["n"] += 1
        return True

    orig = farch.handle_zip_root
    farch.handle_zip_root = fake_handle
    try:
        farch.process_zip_dir(
            None,
            "b",
            "root-farm",
            str(zdir),
            str(tmp_path / "processed"),
            (".mov",),
            0.25,
            0,
            zip_farm_ids={"farm_a.zip": "from-map"},
        )
    finally:
        farch.handle_zip_root = orig
    assert called["n"] == 0  # never archived under a guessed farm


def test_promote_carries_the_context_card(tmp_path):
    proc = tmp_path / "processing"
    proc.mkdir()
    z = _mksettled(proc, "done.zip")
    with open(z + ".archive.json", "w") as fh:
        json.dump({"zip": "done.zip", "entries": {"x.mov": {}}}, fh)
    with open(z + ".context.json", "w") as fh:
        json.dump({"farm_id": "f"}, fh)

    farch.promote_zip(z, str(tmp_path / "processed"))

    done = tmp_path / "processed"
    assert (done / "done.zip").exists()
    assert (done / "done.zip.archive.json").exists()
    assert (done / "done.zip.context.json").exists()
    assert not os.path.exists(proc / "done.zip.context.json")
