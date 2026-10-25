"""Tests for the LLM vision caption stage (farm_media_captions.visual)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_captions as fc


# ------------------------------------------------------------------ frame times
def test_visual_frame_times_default_fracs():
    # 3 frames at 15% / 50% / 85% of a 10s clip
    assert fc.visual_frame_times(10) == [1.5, 5.0, 8.5]


def test_visual_frame_times_custom_fracs():
    assert fc.visual_frame_times(20, (0.25,)) == [5.0]


def test_visual_frame_times_rejects_unusable_duration():
    assert fc.visual_frame_times(0) == []
    assert fc.visual_frame_times(None) == []
    assert fc.visual_frame_times("nonsense") == []
    assert fc.visual_frame_times(-3) == []


# ------------------------------------------------------------- description layout
def test_build_description_visual_before_transcript():
    d = fc.build_description("English trans.", "Context.", "", "Wide hall shot.")
    assert d == "Context.\n\nWide hall shot.\n\nEnglish trans."


def test_build_description_place_then_visual_then_transcript():
    d = fc.build_description("T", "C", "Ilheus, Bahia", "V")
    assert d == "C\n\n\U0001f4cd Ilheus, Bahia\n\nV\n\nT"


def test_build_description_visual_only_is_the_description():
    # A silent clip: the visual gist is all there is.
    assert fc.build_description("", "", "", "Bags stacked in a warehouse.") == (
        "Bags stacked in a warehouse."
    )


def test_build_description_unchanged_without_visual():
    # Backwards compatible: existing callers pass 3 args.
    assert fc.build_description("Hi there.") == "Hi there."
    d = fc.build_description("English trans.", "Farm context.", "Medicilandia, Para")
    assert d == "Farm context.\n\n\U0001f4cd Medicilandia, Para\n\nEnglish trans."


def test_build_description_visual_respects_max_chars():
    long = fc.build_description("t" * 6000, visual="v" * 6000)
    assert len(long) <= fc.DESC_MAX_CHARS


# --------------------------------------------------------------------- idempotency
def _entry(**kw):
    base = {
        "raw_url": "https://example.test/clip.MOV",
        "duration_s": 12.0,
    }
    base.update(kw)
    return base


def test_visual_one_skips_when_already_described(monkeypatch):
    calls = []
    monkeypatch.setattr(fc, "describe_clip", lambda *a, **k: calls.append(1))

    entry = _entry(visual_description="Already seen this one.")
    assert fc.visual_one(entry) is False
    assert calls == []  # CRITICAL: no paid API call on a described clip
    assert entry["visual_description"] == "Already seen this one."


def test_visual_one_skips_after_max_attempts(monkeypatch):
    calls = []
    monkeypatch.setattr(fc, "describe_clip", lambda *a, **k: calls.append(1))

    entry = _entry(visual_attempts=fc.VISUAL_MAX_ATTEMPTS)
    assert fc.visual_one(entry) is False
    assert calls == []


def test_visual_one_records_success(monkeypatch):
    monkeypatch.setattr(
        fc,
        "describe_clip",
        lambda *a, **k: {
            "description": "A woman carries boxes past a pallet.",
            "framing": "wide",
            "setting": "indoor",
            "subjects": ["person", "boxes"],
            "product_in_frame": False,
            "camera": "handheld",
            "model": "gemini-3-flash-preview",
            "frames": 3,
        },
    )
    entry = _entry(description="Earlier description.")
    assert fc.visual_one(entry) is True

    assert entry["visual_description"] == "A woman carries boxes past a pallet."
    assert entry["visual_framing"] == "wide"
    assert entry["visual_setting"] == "indoor"
    assert entry["product_in_frame"] is False
    assert entry["visual_subjects"] == ["person", "boxes"]
    assert entry["visual_attempts"] == 1
    assert "visual_error" not in entry
    # gist appended to the composed description
    assert entry["description"].startswith("Earlier description.")
    assert "A woman carries boxes" in entry["description"]


def test_visual_one_does_not_duplicate_gist(monkeypatch):
    gist = "A woman carries boxes."
    monkeypatch.setattr(fc, "describe_clip", lambda *a, **k: {"description": gist})
    entry = _entry(description=f"Earlier.\n\n{gist}")
    fc.visual_one(entry)
    assert entry["description"].count(gist) == 1


def test_visual_one_counts_attempt_and_records_error(monkeypatch):
    monkeypatch.setattr(fc, "describe_clip", lambda *a, **k: {"error": "HTTP 429"})
    entry = _entry()
    assert fc.visual_one(entry) is True
    assert entry["visual_attempts"] == 1
    assert entry["visual_error"] == "HTTP 429"
    assert "visual_description" not in entry


def test_visual_one_is_retried_until_cap_then_stops(monkeypatch):
    monkeypatch.setattr(fc, "describe_clip", lambda *a, **k: {"error": "boom"})
    entry = _entry()
    for n in range(1, fc.VISUAL_MAX_ATTEMPTS + 1):
        assert fc.visual_one(entry) is True
        assert entry["visual_attempts"] == n
    # now capped -- no further calls
    monkeypatch.setattr(fc, "describe_clip", lambda *a, **k: {"description": "x"})
    assert fc.visual_one(entry) is False


def test_visual_one_requires_a_url(monkeypatch):
    monkeypatch.setattr(fc, "describe_clip", lambda *a, **k: {"description": "x"})
    entry = {"duration_s": 5.0}
    assert fc.visual_one(entry) is True
    assert entry["visual_error"] == "no raw_url/preview_url"


# ----------------------------------------------------------------- describe_clip
def test_describe_clip_without_api_key_errors(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert fc.describe_clip("https://example.test/x.MOV", 10)["error"].startswith(
        "GEMINI_API_KEY"
    )


def test_describe_clip_bad_duration_errors(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    out = fc.describe_clip("https://example.test/x.MOV", None)
    assert "no usable duration" in out["error"]


def test_run_visual_archive_filter_skips_other_sidecars(monkeypatch, tmp_path):
    import json as _json

    zdir = tmp_path / "processing"
    zdir.mkdir()
    for name, clip in (
        ("wanted.zip.archive.json", "A.MOV"),
        ("other.zip.archive.json", "B.MOV"),
    ):
        (zdir / name).write_text(
            _json.dumps({"entries": {clip: {"raw_url": "https://x/y.MOV", "duration_s": 5}}}),
            encoding="utf-8",
        )
    cfg = {"archive": {"roots": [{"zip_dir": str(zdir)}]},
           "captions": {"visual_frame_fracs": [0.5]}}

    seen = []
    monkeypatch.setattr(
        fc, "visual_one", lambda e, *a, **k: seen.append(e["raw_url"]) or False
    )
    fc.run_visual(cfg, archive="wanted")
    assert seen == ["https://x/y.MOV"]  # only the wanted sidecar was touched


def test_vision_model_is_not_retired_2_5():
    # Regression guard: 2.5.x is 404 for new project keys.
    assert "2.5" not in fc.VISION_MODEL
