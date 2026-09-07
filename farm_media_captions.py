#!/usr/bin/env python3
"""Farm Media Captions -- pt->en transcripts, WebVTT captions & descriptions (MAP).

Per governor directive (2026-09-07): before a farm video ships to YouTube,
transcribe its (typically Portuguese) audio and translate to English with
faster-whisper (task="translate"), write a WebVTT caption file beside the MP4,
and make the English translation the video description. After upload the .vtt
is attached as a real YouTube caption track (toggleable, searchable -- no
burned pixels). Already-uploaded videos get the same treatment via backfill.

Design notes
------------
* This worker is deliberately separate from the upload daemon, mirroring how
  farm_media_archive.py sits apart: whisper is CPU/memory-heavy per file and
  the daemon stays dumb (DESIGN.md principles 1 & 7). It ENRICHES sidecars in
  place; the dumb daemon then just uploads whatever the sidecar says.
* Heavy imports (faster_whisper, googleapiclient) are lazy -- the pure helpers
  (VTT building, description assembly) stay importable/testable anywhere.
* Resume-safe: a sidecar that already has `transcript_en` / `caption_track` is
  skipped, so re-runs and interrupted backfills are cheap.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import yaml

LOG = logging.getLogger("farm_media_captions")

DESC_MAX_CHARS = 4400  # YouTube description cap is 5000; keep margin.
DEFAULT_LANG = "pt"
BACKOFF_S = 30


# --------------------------------------------------------------------------- pure helpers
def _ts(seconds: float) -> str:
    """Format seconds as WebVTT cue timestamp (HH:MM:SS.mmm)."""
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def build_vtt(segments) -> str:
    """Render whisper segments to a WebVTT document. Segments must be objects
    with .start/.end/.text (faster-whisper) or dicts with the same keys."""
    cues = []
    for seg in segments:
        start = seg.start if hasattr(seg, "start") else seg["start"]
        end = seg.end if hasattr(seg, "end") else seg["end"]
        text = (seg.text if hasattr(seg, "text") else seg["text"]).strip()
        if not text:
            continue
        cues.append(f"{_ts(start)} --> {_ts(end)}\n{text}")
    if not cues:
        return "WEBVTT\n\n"  # no speech
    return "WEBVTT\n\n" + "\n\n".join(cues) + "\n"


def build_description(transcript: str, existing: str = "") -> str:
    """The video description is the English translation (governor directive).

    Keeps a short existing context line when the transcript is empty (no
    speech / instrumental clip), otherwise the translation stands alone.
    """
    t = (transcript or "").strip()
    if not t:
        return (existing or "").strip()[:DESC_MAX_CHARS]
    return t[:DESC_MAX_CHARS]


# --------------------------------------------------------------------------- whisper
def transcribe_en(mp4: str, model_name: str = "base", language: str = DEFAULT_LANG):
    """Translate audio to English text. Returns (plain_text, segments_list)."""
    from faster_whisper import WhisperModel  # lazy: heavy dep

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    kwargs = {"task": "translate", "vad_filter": True}
    if language:
        kwargs["language"] = language
    seg_it, _info = model.transcribe(mp4, **kwargs)
    segments = list(seg_it)
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, segments


# --------------------------------------------------------------------------- youtube
def _youtube(token_path: str):
    from google.auth.transport.requests import Request  # lazy
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(token_path, None)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def attach_caption(yt, video_id: str, vtt_path: str, lang: str = "en") -> str:
    """Upload a .vtt as the video's caption track. Returns the caption id."""
    from googleapiclient.http import MediaFileUpload  # lazy

    body = {
        "snippet": {
            "videoId": video_id,
            "language": lang,
            "name": "English",
            "isDraft": False,
        }
    }
    media = MediaFileUpload(vtt_path, mimetype="text/vtt", resumable=False)
    resp = yt.captions().insert(part="snippet", body=body, media_body=media).execute()
    return resp.get("id", "")


def set_description(yt, video_id: str, description: str) -> None:
    """Update only the description, preserving the rest of the snippet."""
    got = yt.videos().list(part="snippet", id=video_id).execute().get("items", [])
    if not got:
        raise RuntimeError(f"video {video_id} not found")
    snip = dict(got[0]["snippet"])
    snip["description"] = description
    yt.videos().update(
        part="snippet",
        body={"id": video_id, "snippet": snip},
    ).execute()


# --------------------------------------------------------------------------- sidecar io
def _iter_inbox_videos(inboxes: list[dict]):
    for inbox in inboxes:
        path = inbox.get("path", "")
        if not os.path.isdir(path):
            continue
        for name in sorted(os.listdir(path)):
            if not name.lower().endswith((".mp4", ".mov", ".m4v")):
                continue
            mp4 = os.path.join(path, name)
            sc = mp4 + ".json"
            if not os.path.exists(sc):
                LOG.warning("missing sidecar for %s", mp4)
                continue
            with open(sc, encoding="utf-8") as fh:
                sidecar = json.load(fh)
            yield mp4, sc, sidecar


def _write_sidecar(sc: str, sidecar: dict) -> None:
    tmp = sc + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, sc)


# --------------------------------------------------------------------------- actions
def enrich_one(mp4: str, sc: str, sidecar: dict, model: str, lang: str) -> bool:
    """Transcribe+translate to English; write .vtt; set sidecar description.
    Returns True if the sidecar was modified. Skips if already enriched."""
    if sidecar.get("transcript_en") is not None and sidecar.get("vtt"):
        return False
    LOG.info("transcribing %s", os.path.basename(mp4))
    text, segments = transcribe_en(mp4, model_name=model, language=lang)
    vtt_path = mp4 + ".en.vtt"
    with open(vtt_path, "w", encoding="utf-8") as fh:
        fh.write(build_vtt(segments))
    sidecar["transcript_en"] = text
    sidecar["vtt"] = os.path.basename(vtt_path)
    # Preserve any hand-written summary for reference, then make the EN
    # translation the live description (governor directive).
    if sidecar.get("description"):
        sidecar.setdefault("description_original", sidecar["description"])
    sidecar["description"] = build_description(
        text, sidecar.get("description_original", "")
    )
    _write_sidecar(sc, sidecar)
    return True


def attach_one(yt, sc: str, sidecar: dict, inbox_path: str) -> bool:
    """Attach caption track + push EN description for an uploaded video."""
    yt_id = sidecar.get("yt_id")
    if not yt_id:
        return False
    if sidecar.get("caption_track"):
        return False
    vtt_rel = sidecar.get("vtt")
    if not vtt_rel:
        return False
    vtt_path = os.path.join(inbox_path, vtt_rel)
    if not os.path.exists(vtt_path):
        LOG.warning("vtt missing for %s (%s)", yt_id, vtt_path)
        return False
    cid = attach_caption(yt, yt_id, vtt_path)
    sidecar["caption_track"] = cid
    if sidecar.get("description") and not sidecar.get("description_uploaded"):
        # Already-uploaded video: push the EN description too.
        set_description(yt, yt_id, sidecar["description"])
        sidecar["description_uploaded"] = True
    _write_sidecar(sc, sidecar)
    LOG.info("captions attached to %s (id %s)", yt_id, cid)
    return True


def run_enrich(cfg: dict, model: str, lang: str) -> int:
    changed = 0
    for mp4, sc, sidecar in _iter_inbox_videos(cfg.get("inboxes", [])):
        try:
            if enrich_one(mp4, sc, sidecar, model, lang):
                changed += 1
        except Exception as exc:  # keep the pass going
            LOG.error("enrich failed for %s: %s", os.path.basename(mp4), exc)
    return changed


def run_backfill(cfg: dict, token_path: str, model: str, lang: str) -> int:
    """Enrich every inbox video missing a transcript, then attach captions +
    description to every already-uploaded one. Resume-safe + 429-aware."""
    yt = _youtube(token_path)
    done = 0
    for inbox in cfg.get("inboxes", []):
        path = inbox.get("path", "")
        for mp4, sc, sidecar in _iter_inbox_videos([inbox]):
            try:
                enrich_one(mp4, sc, sidecar, model, lang)
                if sidecar.get("yt_id") and not sidecar.get("caption_track"):
                    while True:
                        try:
                            if attach_one(yt, sc, sidecar, path):
                                done += 1
                            break
                        except Exception as exc:  # 429 / rate-limit retry
                            low = str(exc).lower()
                            if (
                                "quota" not in low
                                and "429" not in low
                                and "ratelimit" not in low
                            ):
                                LOG.error(
                                    "attach failed %s: %s", os.path.basename(mp4), exc
                                )
                                break
                            LOG.warning("quota hit; sleeping %ss: %s", BACKOFF_S, exc)
                            time.sleep(BACKOFF_S)
                time.sleep(1)  # gentle pacing between videos
            except Exception as exc:
                LOG.error("backfill failed for %s: %s", os.path.basename(mp4), exc)
    return done


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Farm Media Captions (pt->en)")
    ap.add_argument(
        "--config", default="/opt/truesight_autopilot/farm_media_daemon/media_archive_daemon_config.yaml"
    )
    ap.add_argument(
        "--token", default="/opt/truesight_autopilot/config/youtube/youtube_token.json"
    )
    ap.add_argument("--model", default="base")
    ap.add_argument("--lang", default=DEFAULT_LANG)
    ap.add_argument("--log-file", default="/tmp/farm_media_captions.log")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser(
        "enrich", help="transcribe+translate all sidecars missing a transcript"
    )
    sub.add_parser(
        "backfill", help="enrich, then attach captions+descriptions to uploaded videos"
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(args.log_file),
            logging.StreamHandler(sys.stderr),
        ],
    )
    cfg = load_config(args.config)
    cap = cfg.get("captions") or {}
    model = args.model or cap.get("model", "base")
    lang = args.lang or cap.get("language", DEFAULT_LANG)
    if args.action == "enrich":
        run_enrich(cfg, model, lang)
    else:
        run_backfill(cfg, args.token, model, lang)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
