import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_captions as fc


def test_ts():
    assert fc._ts(0) == "00:00:00.000"
    assert fc._ts(2.5) == "00:00:02.500"
    assert fc._ts(3661.25) == "01:01:01.250"


class _Seg:
    def __init__(self, s, e, t):
        self.start, self.end, self.text = s, e, t


def test_build_vtt_basic():
    vtt = fc.build_vtt([_Seg(0.5, 2.0, "Hello world")])
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.500 --> 00:00:02.000" in vtt
    assert "Hello world" in vtt


def test_build_vtt_skips_blank_cues():
    vtt = fc.build_vtt([_Seg(0, 1, "   "), _Seg(2, 3, "hi")])
    assert vtt.count("-->") == 1


def test_build_description():
    assert fc.build_description("Hi there.") == "Hi there."
    assert fc.build_description("", "context line") == "context line"
    long = fc.build_description("x" * 6000)
    assert len(long) <= fc.DESC_MAX_CHARS


def test_enrich_one_writes_vtt_and_swaps_description(tmp_path, monkeypatch):
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"fake")
    sc = tmp_path / "clip.mp4.json"
    sidecar = {"file": "clip.mp4", "description": "orig ctx", "yt_id": None}
    json.dump(sidecar, open(sc, "w"))

    class Seg:
        start = 0.0
        end = 1.0
        text = "Ola mundo"

    def fake_transcribe(mp4, model_name="base", language="pt"):
        return "Ola mundo", [Seg()]

    monkeypatch.setattr(fc, "transcribe_en", fake_transcribe)
    changed = fc.enrich_one(str(mp4), str(sc), sidecar, "base", "pt")
    assert changed is True
    out = json.load(open(sc))
    assert out["transcript_en"] == "Ola mundo"
    assert out["vtt"] == "clip.mp4.en.vtt"
    assert out["description_original"] == "orig ctx"
    assert out["description"] == "Ola mundo"
    assert (tmp_path / "clip.mp4.en.vtt").exists()


def test_enrich_one_skips_when_already_done(tmp_path, monkeypatch):
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"fake")
    sc = tmp_path / "clip.mp4.json"
    sidecar = {"file": "clip.mp4", "transcript_en": "x", "vtt": "clip.mp4.en.vtt"}
    json.dump(sidecar, open(sc, "w"))

    def boom(*a, **k):
        raise AssertionError("should not transcribe again")

    monkeypatch.setattr(fc, "transcribe_en", boom)
    assert fc.enrich_one(str(mp4), str(sc), sidecar, "base", "pt") is False
