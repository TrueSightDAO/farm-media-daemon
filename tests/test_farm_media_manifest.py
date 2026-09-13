import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_manifest as m


def test_parse_gps_decimal():
    assert m._parse_gps("-3.4146, -52.6285") == (-3.4146, -52.6285, "-3.4146, -52.6285")


def test_parse_gps_dms_string():
    """The bug: exiftool DMS output must resolve to decimal, not None."""
    lat, lon, raw = m._parse_gps(
        "3 deg 33' 25.20\" S, 51 deg 6' 13.32\" W, 108.156 m Above Sea Level"
    )
    assert raw is not None
    assert lat is not None and lon is not None
    assert abs(lat - (-3.5570)) < 0.001
    assert abs(lon - (-51.1037)) < 0.001


def test_parse_gps_matches_geo_parser():
    """Manifest parsing must agree with the daemon's own geo parser."""
    import farm_media_geo as g

    s = "3 deg 24' 56.52\" S, 52 deg 36' 54.00\" W"
    assert m._parse_gps(s) == g.parse_gps(s)


def test_parse_gps_none_and_junk():
    assert m._parse_gps(None) == (None, None, None)
    assert m._parse_gps("") == (None, None, None)
    lat, lon, raw = m._parse_gps("no gps here")
    assert lat is None and lon is None and raw == "no gps here"


# --- PR4: --with-gallery + yt_id backfill + parity -------------------------


def _seed_inbox(tmp_path, items):
    """Write a fake inbox: each spec is (filename, sidecar_dict_or_None)."""
    for fname, side in items:
        p = os.path.join(tmp_path, fname)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x")
        if side is not None:
            with open(p + ".json", "w", encoding="utf-8") as fh:
                json.dump(side, fh)


def test_build_manifest_reads_sidecars():
    import tempfile

    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "farm-x"))
    _seed_inbox(
        os.path.join(d, "farm-x"),
        [
            (
                "A.MOV",
                {
                    "file": "A.MOV",
                    "yt_id": "aaa",
                    "duration_s": 5.0,
                    "description": "A cacao tree",
                },
            ),
            ("P.HEIC", {"file": "P.HEIC", "description": "A pod"}),
        ],
    )
    man = m.build_manifest("farm-x", d)
    assert man["farm_id"] == "farm-x"
    assert {i["file"] for i in man["items"]} == {"A.MOV", "P.HEIC"}
    assert next(i for i in man["items"] if i["file"] == "A.MOV")["yt_id"] == "aaa"


def test_build_manifest_backfills_yt_id():
    """A manifest built after an upload carries the sidecar's yt_id (the backfill)."""
    import tempfile

    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "farm-x"))
    _seed_inbox(os.path.join(d, "farm-x"), [("A.MOV", {"file": "A.MOV"})])
    before = m.build_manifest("farm-x", d)
    assert before["items"][0]["yt_id"] is None
    with open(os.path.join(d, "farm-x", "A.MOV.json"), "w", encoding="utf-8") as fh:
        json.dump({"file": "A.MOV", "yt_id": "zzz"}, fh)
    after = m.build_manifest("farm-x", d)
    assert after["items"][0]["yt_id"] == "zzz"


def test_build_manifest_missing_inbox_raises():
    try:
        m.build_manifest("nope", "/no/such/dir")
    except FileNotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected FileNotFoundError")


def test_strip_private_drops_underscore_keys():
    man = {"farm_id": "f", "items": [{"file": "A.MOV", "_path": "/x/A.MOV"}]}
    assert m.strip_private(man)["items"][0] == {"file": "A.MOV"}


def test_gallery_from_manifest_publishes_only_uploaded_videos():
    man = {
        "farm_id": "f",
        "items": [
            {"basename": "A.MOV", "file": "A.MOV", "yt_id": "a"},
            {"basename": "B.MOV", "file": "B.MOV", "yt_id": None},
        ],
    }
    doc = m.gallery_from_manifest(man, "f")
    assert [e["videoId"] for e in doc["gallery"]] == ["a"]


def test_parity_ok():
    man = {"items": [{"yt_id": "a"}, {"yt_id": None}]}
    doc = {"gallery": [{"type": "youtube", "videoId": "a"}]}
    p = m.parity(man, doc)
    assert p["sidecars_with_yt_id"] == p["manifest_yt_id"] == p["gallery_youtube"] == 1
    assert p["ok"] is True


def test_parity_mismatch_flags_unpublished_upload():
    man = {"items": [{"yt_id": "a"}, {"yt_id": "b"}]}
    doc = {"gallery": [{"type": "youtube", "videoId": "a"}]}
    p = m.parity(man, doc)
    assert p["ok"] is False
    assert p["sidecars_with_yt_id"] == 2 and p["gallery_youtube"] == 1


def test_main_with_gallery_writes_both(tmp_path):
    inbox = os.path.join(str(tmp_path), "inbox")
    os.makedirs(os.path.join(inbox, "farm-x"))
    _seed_inbox(
        os.path.join(inbox, "farm-x"),
        [
            ("A.MOV", {"file": "A.MOV", "yt_id": "a", "duration_s": 5.0}),
            ("P.HEIC", {"file": "P.HEIC", "description": "A pod"}),
        ],
    )
    outdir = os.path.join(str(tmp_path), "out")
    rc = m.main(
        [
            "farm-x",
            "--inbox",
            inbox,
            "--outdir",
            outdir,
            "--with-gallery",
            "--no-aspect",
        ]
    )
    assert rc == 0
    assert os.path.exists(os.path.join(outdir, "farm-x.json"))
    gal = os.path.join(outdir, "galleries", "farm-x.json")
    assert os.path.exists(gal)
    with open(gal, encoding="utf-8") as fh:
        doc = json.load(fh)
    assert sum(1 for e in doc["gallery"] if e["type"] == "youtube") == 1
    with open(os.path.join(outdir, "farm-x.json"), encoding="utf-8") as fh:
        man = json.load(fh)
    assert all("_path" not in i for i in man["items"])


def test_main_parity_gate_returns_2(tmp_path, monkeypatch):
    inbox = os.path.join(str(tmp_path), "inbox")
    os.makedirs(os.path.join(inbox, "farm-x"))
    _seed_inbox(
        os.path.join(inbox, "farm-x"),
        [("A.MOV", {"file": "A.MOV", "yt_id": "a"})],
    )
    outdir = os.path.join(str(tmp_path), "out")

    # Force the published gallery to hide the uploaded clip -> parity must fail.
    monkeypatch.setattr(
        m, "gallery_from_manifest", lambda *a, **k: {"schemaVersion": 1, "gallery": []}
    )
    rc = m.main(
        [
            "farm-x",
            "--inbox",
            inbox,
            "--outdir",
            outdir,
            "--with-gallery",
            "--no-aspect",
        ]
    )
    assert rc == 2
