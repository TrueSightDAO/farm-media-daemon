import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_gallery as g  # noqa: E402


def _item(**kw):
    base = {
        "file": "IMG_0001.MOV",
        "basename": "IMG_0001.MOV",
        "captured_at": "2026-09-09T14:00:00",
        "yt_id": None,
        "title": "Farm - IMG_0001",
        "duration_s": 10.0,
        "description": None,
        "transcript_en": None,
        "place_name": "Anapu",
    }
    base.update(kw)
    return base


def test_sanitize_drops_amara_hallucination():
    assert g.sanitize_transcript("Legendas pela comunidade de Amara.org") == ""


def test_sanitize_keeps_real_speech_around_hallucination():
    out = g.sanitize_transcript("Bom dia. Legendas pela comunidade de Amara.org")
    assert "Bom dia" in out
    assert "amara" not in out.lower()


def test_extract_transcript_prefers_transcript_en():
    assert g.extract_transcript({"transcript_en": "hello"}) == "hello"


def test_extract_transcript_from_description_body():
    desc = "Intro line.\n\n" + g.PIN + " Anapu, Para\n\nEu falei do cacau."
    assert g.extract_transcript({"description": desc}) == "Eu falei do cacau."


def test_description_only_intro_has_no_transcript():
    assert g.extract_transcript({"description": "Just an intro."}) == ""


def test_classify_aspect():
    assert g.classify_aspect(1080, 1920) == "portrait"
    assert g.classify_aspect(1920, 1080) == "landscape"
    assert g.classify_aspect(500, 500) == "square"


def test_probe_aspect_tolerates_missing_ffprobe():
    def boom(*a, **k):
        raise FileNotFoundError("ffprobe")

    assert g.probe_aspect("/nope.mp4", runner=boom) is None


def test_build_intro():
    assert g.build_intro("2026-09-09T14:54:16") == "Site walk 9 September 2026"
    assert g.build_intro(None) == ""


def test_caption_with_transcript():
    c = g.build_caption(
        _item(transcript_en="Bom dia"),
        intro="Site walk 9 September 2026",
        place="Anapu, Para",
        plot="N-06-66",
    )
    head = "Site walk 9 September 2026" + g.DOT + "Anapu, Para" + g.DOT + "plot N-06-66"
    assert c == head + g.DASH + "Bom dia"


def test_caption_boilerplate_on_empty_transcript():
    c = g.build_caption(
        _item(), intro="Site walk 9 September 2026", place="Anapu, Para"
    )
    assert c == "Site walk 9 September 2026" + g.DOT + "Anapu, Para"
    assert g.DASH.strip() not in c


def test_caption_truncation():
    c = g.build_caption(_item(transcript_en="palavra " * 100), intro="X")
    assert c.endswith(g.ELLIPSIS)
    assert len(c) <= len("X") + len(g.DASH) + g.CAPTION_MAX_CHARS + len(g.ELLIPSIS)


def test_build_title_appends_duration_once():
    assert (
        g.build_title({"title": "Cristo Rei", "duration_s": 8.49}) == "Cristo Rei (8 s)"
    )
    assert g.build_title({"title": "Already (5 s)", "duration_s": 9}) == "Already (5 s)"
    assert (
        g.build_title({"file": "IMG_0007.MOV", "basename": "IMG_0007.MOV"})
        == "IMG_0007"
    )


def test_gallery_skips_unpublished_video():
    items = [
        _item(file="A.MOV", basename="A.MOV", yt_id=None),
        _item(file="B.MOV", basename="B.MOV", yt_id="abc"),
    ]
    doc = g.build_gallery(items, "f")
    assert [e["videoId"] for e in doc["gallery"]] == ["abc"]


def test_gallery_order_is_capture_time_stable():
    items = [
        _item(
            file="B.MOV", basename="B.MOV", yt_id="b", captured_at="2026-09-09T15:00:00"
        ),
        _item(
            file="A.MOV", basename="A.MOV", yt_id="a", captured_at="2026-09-09T14:00:00"
        ),
    ]
    doc = g.build_gallery(items, "f")
    assert [e["videoId"] for e in doc["gallery"]] == ["a", "b"]


def test_gallery_is_idempotent():
    items = [
        _item(file="A.MOV", basename="A.MOV", yt_id="a", transcript_en="Oi"),
        _item(file="P.HEIC", basename="P.HEIC", description="A cacao tree"),
    ]
    one = json.dumps(g.build_gallery(items, "f"), sort_keys=True)
    two = json.dumps(g.build_gallery(items, "f"), sort_keys=True)
    assert one == two


def test_gallery_schema_and_hero_from_first_image():
    items = [
        _item(file="A.MOV", basename="A.MOV", yt_id="a"),
        _item(file="P.HEIC", basename="P.HEIC", description="A cacao tree"),
    ]
    doc = g.build_gallery(items, "cacau-na-veia-pacaje")
    assert doc["schemaVersion"] == 1
    assert doc["hero"]["type"] == "image"
    assert doc["hero"]["src"].endswith("cacau-na-veia-pacaje-P.jpg")
    types = [e["type"] for e in doc["gallery"]]
    assert types == ["youtube", "image"]


def test_gallery_yt_entry_has_videoId_and_title_not_id():
    doc = g.build_gallery([_item(yt_id="zzz")], "f")
    entry = doc["gallery"][0]
    assert entry["type"] == "youtube"
    assert entry["videoId"] == "zzz"
    assert "id" not in entry
    assert entry["title"]


def test_aspect_probe_injected():
    doc = g.build_gallery([_item(yt_id="z")], "f", aspect_probe=lambda item: "portrait")
    assert doc["gallery"][0]["aspect"] == "portrait"


def test_manifest_loader_roundtrip(tmp_path=None):
    import tempfile

    payload = {
        "farm_id": "sitio-torres-pacaja-para",
        "plots": ["N-06-66"],
        "items": [_item(file="A.MOV", basename="A.MOV", yt_id="a")],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        path = fh.name
    try:
        farm_id, plots, items = g.load_manifest_items(path)
    finally:
        os.unlink(path)
    assert farm_id == "sitio-torres-pacaja-para"
    assert plots == ["N-06-66"]
    assert items[0]["yt_id"] == "a"
    assert items[0]["_farm_id"] == "sitio-torres-pacaja-para"
