"""Dense frame sampling + per-frame cacao index (visual enrichment).

Regression cover for two defects found 2026-11-26:

1. The first dense sampler put every sample in the opening seconds of a long
   clip (12 frames inside the first 12s -> ~2% coverage of a 10-minute file),
   so the "index" described the intro and nothing else.
2. `describe_clip` learned `dense=True` but `visual_one` never passed it and
   never persisted `cacao_stage` / `frame_index`, so the new fields were
   silently dropped on the floor -- every sidecar kept an empty value.

These assert behaviour, not implementation: spread across the clip, bounded
count, and the dense flag actually reaching the frame-time calculation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import farm_media_captions as fc  # noqa: E402


def test_dense_spans_whole_clip():
    """Last sample must land near the end -- not in the opening seconds."""
    for dur in (60, 360, 600):
        times = fc.visual_frame_times_dense(dur)
        assert times, f"no samples for {dur}s"
        assert times[-1] / dur > 0.90, f"{dur}s clip covered only to {times[-1]}s"
        assert times[0] > 0, "sample at t=0 is unreliable (black/first-frame)"


def test_dense_is_bounded():
    """A long clip must not blow up the frame count (API + CPU cost)."""
    for dur in (1, 12, 120, 3600):
        times = fc.visual_frame_times_dense(dur)
        assert len(times) <= 12, f"{dur}s produced {len(times)} samples"
        assert len(times) >= 1


def test_dense_monotonic_and_in_range():
    times = fc.visual_frame_times_dense(97.5)
    assert times == sorted(times), "samples must be in chronological order"
    assert all(0 < t < 97.5 for t in times), times


def test_dense_handles_bad_duration():
    """Unusable durations yield [] rather than raising."""
    for bad in (0, None, "", "abc", -5):
        assert fc.visual_frame_times_dense(bad) == []


def test_describe_clip_dense_reaches_frame_times(monkeypatch):
    """`dense=True` must actually change which timestamps get sampled.

    This is the specific bug: the flag existed on describe_clip but did not
    change the sampler, so dense and sparse produced identical output.
    """
    seen = {}

    def fake_times(duration_s, fracs=None):
        seen["called"] = True
        return [1.0, 2.0]

    monkeypatch.setattr(fc, "visual_frame_times_dense", fake_times)
    monkeypatch.setattr(fc, "_ffmpeg_frame", lambda *a, **k: False)

    fc.describe_clip("http://example.invalid/x.MOV", 10.0, "m", dense=True)
    assert seen.get("called"), "dense=True did not use the dense sampler"


def test_visual_schema_declares_new_fields():
    """The schema must advertise what the prompt asks for."""
    props = fc.VISUAL_SCHEMA["properties"]
    assert "cacao_stage" in props
    assert "frame_index" in props
    assert "frame_index" in fc.VISUAL_SCHEMA["required"]
    assert "cacao_stage" in fc.VISUAL_SCHEMA["required"]


def test_grade_is_never_requested():
    """Bulk/gourmet is a lot attribute, NOT a visible one.

    If this ever starts failing, someone has re-added a guessed grade to the
    vision prompt -- which would manufacture an indefensible claim.
    """
    prompt = fc.VISUAL_PROMPT.lower()
    assert "never a grade" in prompt
    assert "bulk/gourmet" in prompt
    for prop in fc.VISUAL_SCHEMA["properties"]:
        assert prop != "grade", "grade must not be a vision output field"
