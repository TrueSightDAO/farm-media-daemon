#!/usr/bin/env python3
"""Farm Media Captions -- pt->en transcripts, WebVTT captions & descriptions (MAP).

Per governor directive (2026-09-07): before a farm video ships to YouTube,
transcribe its (typically Portuguese) audio and translate to English with
faster-whisper (task="translate"), write a WebVTT caption file beside the MP4,
and make the English translation the video description. After upload the .vtt
is attached as a real YouTube caption track (toggleable, searchable -- no
burned pixels). Already-uploaded videos get the same treatment via backfill.

When the sidecar carries GPS the description gains a 3rd location line
("\U0001f4cd Place, Region") reverse-geocoded from the Google Geocoding API
(see farm_media_geo.py); the resolved place is stored back in the sidecar as
place_name / place_address / place_id. Runs only when a Maps key is present.

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
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import yaml

try:
    import farm_media_geo as geo
except Exception:  # pragma: no cover - importable standalone
    geo = None

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


def build_description(
    transcript: str, existing: str = "", place: str = "", visual: str = ""
) -> str:
    """The video description is the English translation (governor directive).

    Layout (governor, 2026-09-07; visual line added 2026-10-25): context intro,
    then a place line, then the visual gist, then the English translation. The
    place and visual lines are omitted when absent; the context intro is kept
    when the transcript is empty (no speech / instrumental clip). Visual sits
    before the transcript because it is scene-setting -- for a silent clip it
    IS the description.
    """
    t = (transcript or "").strip()
    ex = (existing or "").strip()
    pl = (place or "").strip()
    vi = (visual or "").strip()
    parts = []
    if ex:
        parts.append(ex)
    if pl:
        # Pin location when a farm video actually has one (fallback: no line).
        parts.append("\U0001f4cd " + pl)  # U+1F4CD location pin
    if vi:
        parts.append(vi)
    if t:
        parts.append(t)
    if not parts:
        return ""
    return "\n\n".join(parts)[:DESC_MAX_CHARS]


# --------------------------------------------------------------- vision stage
# Governor directive (2026-10-25): a video's description should carry what is
# VISIBLE, not only what is said. Many farm clips carry no speech at all, so a
# transcript-only description left them effectively uncaptioned.
#
# Gemini Flash reads ~3 frames sampled across the clip; ffmpeg pulls those
# frames straight from the S3 URL (no full download). Results land in the
# sidecar as `visual_description` plus structured shot-list fields, and the
# composed `description` gains the visual gist.
#
# IDEMPOTENCE IS THE POINT: a clip that already has `visual_description` is
# skipped, so restarts and re-runs never re-bill. Failures bump
# `visual_attempts` and stop after VISUAL_MAX_ATTEMPTS so one broken clip
# cannot burn budget forever.
#
# MODEL NOTES (learned the hard way against the live API):
#   * gemini-2.5-flash is 404 for new project keys -- do not "fix" this by
#     reverting to it.
#   * The 3.x thinking models SILENTLY TRUNCATE the caption because
#     thoughtsTokenCount eats the output budget, while still reporting
#     finishReason=STOP. Hence thinkingBudget=0 below; without it you get
#     half-sentences that look like success.
VISION_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
VISION_MODEL = os.environ.get("FARM_MEDIA_GEMINI_MODEL", "gemini-3-flash-preview")
VISUAL_FRAME_FRACS = (0.15, 0.50, 0.85)
VISUAL_MAX_ATTEMPTS = 3
VIDEO_EXTS = (".mp4", ".mov", ".m4v")
VISUAL_PROMPT = (
    "These are {n} frames sampled in order (early/middle/late) from ONE short "
    "handheld video. Describe only what is visibly true in the frames. Do not "
    "speculate about intent, emotion, off-screen activity, or names.\n\n"
    "Return JSON with:\n"
    '- "description": one or two plain sentences giving the visual gist.\n'
    '- "framing": "wide" | "medium" | "close" | "mixed"\n'
    '- "setting": "indoor" | "outdoor" | "mixed"\n'
    '- "subjects": short lowercase tags for who/what is visible.\n'
    '- "product_in_frame": true only if cacao product (beans, nibs, bars, '
    "pouches, bags or boxes of product) is identifiable; false if only "
    "people, environment or generic packaging.\n"
    '- "camera": "handheld" | "static" | "unclear"'
)
VISUAL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "description": {"type": "STRING"},
        "framing": {"type": "STRING"},
        "setting": {"type": "STRING"},
        "subjects": {"type": "ARRAY", "items": {"type": "STRING"}},
        "product_in_frame": {"type": "BOOLEAN"},
        "camera": {"type": "STRING"},
    },
    "required": [
        "description",
        "framing",
        "setting",
        "subjects",
        "product_in_frame",
        "camera",
    ],
}


def visual_frame_times(duration_s, fracs=VISUAL_FRAME_FRACS) -> list:
    """Timestamps (seconds) to sample -- [] when the duration is unusable."""
    try:
        dur = float(duration_s or 0)
    except (TypeError, ValueError):
        return []
    if dur <= 0:
        return []
    return [round(dur * f, 3) for f in fracs]


def _ffmpeg_frame(url: str, ts: float, out: str, timeout: int = 180) -> bool:
    """Grab one frame at `ts` seconds straight from a URL into `out` (JPEG)."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-ss",
        f"{ts:.3f}",
        "-i",
        url,
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "-y",
        out,
    ]
    try:
        run = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("ffmpeg failed for %s @%.2fs: %s", url, ts, exc)
        return False
    if run.returncode != 0 or not os.path.exists(out) or not os.path.getsize(out):
        LOG.warning(
            "ffmpeg produced no frame for %s @%.2fs: %s",
            url,
            ts,
            (run.stderr or b"")[:200],
        )
        return False
    return True


def describe_clip(
    raw_url: str, duration_s, model: str = "", fracs=VISUAL_FRAME_FRACS
) -> dict:
    """Sample frames and ask Gemini what is visible. {"error": ...} on failure."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return {"error": "GEMINI_API_KEY not set"}
    times = visual_frame_times(duration_s, fracs)
    if not times:
        return {"error": f"no usable duration ({duration_s!r})"}
    use_model = model or VISION_MODEL
    tmp = tempfile.mkdtemp(prefix="fm_visual_")
    try:
        parts = []
        for i, ts in enumerate(times):
            jpg = os.path.join(tmp, f"f{i}.jpg")
            if not _ffmpeg_frame(raw_url, ts, jpg):
                continue
            with open(jpg, "rb") as fh:
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": base64.b64encode(fh.read()).decode(),
                        }
                    }
                )
        if not parts:
            return {"error": "no frames extracted"}
        body = {
            "contents": [
                {"parts": parts + [{"text": VISUAL_PROMPT.format(n=len(parts))}]}
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 600,
                # See MODEL NOTES above: without this the caption truncates.
                "thinkingConfig": {"thinkingBudget": 0},
                "responseMimeType": "application/json",
                "responseSchema": VISUAL_SCHEMA,
            },
        }
        req = urllib.request.Request(
            f"{VISION_ENDPOINT}/{use_model}:generateContent",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.load(resp)
        cand = (payload.get("candidates") or [{}])[0]
        text = "".join(
            p.get("text", "") for p in (cand.get("content", {}).get("parts") or [])
        ).strip()
        if not text:
            return {"error": f"empty response (finish={cand.get('finishReason')})"}
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            return {"error": f"non-JSON response: {text[:120]}"}
        out["model"] = use_model
        out["frames"] = len(parts)
        return out
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {(exc.read() or b'')[:160]}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def visual_one(entry: dict, model: str = "", fracs=VISUAL_FRAME_FRACS) -> bool:
    """Describe ONE sidecar entry in place. True when the dict changed.

    Idempotent: an entry that already has `visual_description` is left alone, so
    re-runs and restarts cost nothing.

    Note on ordering: an archived entry stores ONE already-composed
    `description` string (its transcript is not separable here), so the gist is
    appended to it. build_description() is the function that puts the visual
    line BEFORE the transcript, and it is what the enrich path uses.
    """
    if not isinstance(entry, dict):
        return False
    if (entry.get("visual_description") or "").strip():
        return False
    if int(entry.get("visual_attempts") or 0) >= VISUAL_MAX_ATTEMPTS:
        return False
    url = entry.get("raw_url") or entry.get("preview_url")
    if not url:
        entry["visual_error"] = "no raw_url/preview_url"
        return True
    out = describe_clip(url, entry.get("duration_s"), model, fracs)
    entry["visual_attempts"] = int(entry.get("visual_attempts") or 0) + 1
    if out.get("error"):
        entry["visual_error"] = out["error"]
        return True
    gist = (out.get("description") or "").strip()
    if gist:
        entry["visual_description"] = gist
        desc = (entry.get("description") or "").strip()
        if gist not in desc:
            entry["description"] = (
                f"{desc}\n\n{gist}"[:DESC_MAX_CHARS] if desc else gist[:DESC_MAX_CHARS]
            )
    entry["visual_framing"] = out.get("framing")
    entry["visual_setting"] = out.get("setting")
    entry["visual_subjects"] = out.get("subjects") or []
    entry["product_in_frame"] = out.get("product_in_frame")
    entry["visual_camera"] = out.get("camera")
    entry["visual_model"] = out.get("model")
    entry["visual_recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry.pop("visual_error", None)
    return True


def _iter_archive_sidecars(cfg: dict):
    """Yield (path, data) for every <zip>.archive.json under archive roots."""
    roots = (cfg.get("archive") or {}).get("roots") or []
    seen = set()
    for root in roots:
        for key in ("zip_dir", "processed_dir"):
            d = root.get(key)
            if not d or d in seen or not os.path.isdir(d):
                continue
            seen.add(d)
            for name in sorted(os.listdir(d)):
                if not name.endswith(".archive.json"):
                    continue
                path = os.path.join(d, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        yield path, json.load(fh)
                except (OSError, json.JSONDecodeError) as exc:
                    LOG.warning("unreadable %s: %s", path, exc)


def _write_json_atomic(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def run_visual(
    cfg: dict,
    model: str = "",
    limit: int = 0,
    dry_run: bool = False,
    archive: str = "",
) -> int:
    """LLM-caption archived clips still missing `visual_description`.

    `archive` narrows the sweep to sidecars whose filename contains that
    substring (e.g. "ilheus_warehouse"), so a targeted backfill does not bill
    for every clip in the estate.
    """
    fracs = tuple(
        (cfg.get("captions") or {}).get("visual_frame_fracs") or VISUAL_FRAME_FRACS
    )
    done = skipped = failed = 0
    for path, data in _iter_archive_sidecars(cfg):
        if archive and archive not in os.path.basename(path):
            continue
        entries = data.get("entries") or {}
        changed = False
        for name in sorted(entries):
            if not name.lower().endswith(VIDEO_EXTS):
                continue
            entry = entries[name]
            if not isinstance(entry, dict):
                continue
            if (entry.get("visual_description") or "").strip():
                skipped += 1
                continue
            if int(entry.get("visual_attempts") or 0) >= VISUAL_MAX_ATTEMPTS:
                LOG.warning("%s: gave up after %d attempts", name, VISUAL_MAX_ATTEMPTS)
                skipped += 1
                continue
            if limit and (done + failed) >= limit:
                break
            if dry_run:
                LOG.info(
                    "[dry-run] would describe %s (%ss)", name, entry.get("duration_s")
                )
                done += 1
                continue
            LOG.info("describing %s (%ss)", name, entry.get("duration_s"))
            if visual_one(entry, model, fracs):
                changed = True
                if (entry.get("visual_description") or "").strip():
                    done += 1
                else:
                    failed += 1
                    LOG.warning("%s: %s", name, entry.get("visual_error"))
        if changed and not dry_run:
            _write_json_atomic(path, data)
            LOG.info("wrote %s", path)
    LOG.info("visual done: %d described, %d skipped, %d failed", done, skipped, failed)
    return done




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


_GEO_CACHE = None


def _load_geo_key(env_name: str) -> None:
    """Pull the Maps key from a named env var (config geo.api_key_env)."""
    if not env_name:
        return
    val = os.environ.get(env_name)
    if val and val.startswith("AIza") and not os.environ.get("GOOGLE_MAPS_API_KEY"):
        os.environ["GOOGLE_MAPS_API_KEY"] = val


def _geo_cache():
    """Process-wide dedupe cache for reverse geocoding (farms share points)."""
    global _GEO_CACHE
    if _GEO_CACHE is None and geo is not None:
        _GEO_CACHE = geo.GeoCache(
            path=os.environ.get(
                "FARM_MEDIA_GEO_CACHE", "/tmp/farm_media_geo_cache.json"
            )
        )
    return _GEO_CACHE


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
    # Reverse-geocode GPS -> place label when present (never raises; no key or
    # no GPS => no place line, description falls back gracefully).
    place = ""
    if geo is not None:
        gps = sidecar.get("gps")
        lat, lon, _raw = geo.parse_gps(gps)
        if lat is not None and lon is not None:
            cache = _geo_cache()
            res = cache.reverse(lat, lon)
            if res:
                place = geo.place_label(lat, lon, cache) or ""
                sidecar["place_name"] = res.get("place_name")
                sidecar["place_address"] = res.get("formatted_address")
                sidecar["place_id"] = res.get("place_id")
    sidecar["description"] = build_description(
        text, sidecar.get("description_original", ""), place
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
    description to every already-uploaded one. Resume-safe + quota-aware:
    YouTube's daily caption quota is a hard wall, so on quotaExceeded the pass
    stops (re-run after the reset picks up where it left off) instead of
    spinning in a retry loop until the quota window rolls over."""
    yt = _youtube(token_path)
    done = 0
    for inbox in cfg.get("inboxes", []):
        path = inbox.get("path", "")
        for mp4, sc, sidecar in _iter_inbox_videos([inbox]):
            try:
                enrich_one(mp4, sc, sidecar, model, lang)
                if sidecar.get("yt_id") and not sidecar.get("caption_track"):
                    # Silent videos transcribe to an empty transcript; attaching
                    # an empty VTT wastes scarce daily quota, so skip them.
                    if not (sidecar.get("transcript_en") or "").strip():
                        LOG.info("skipping attach for silent %s", os.path.basename(mp4))
                        continue
                    retries = 0
                    while True:
                        try:
                            if attach_one(yt, sc, sidecar, path):
                                done += 1
                            break
                        except Exception as exc:  # 429 / rate-limit / quota
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
                            if "quota" in low:
                                # Hard daily wall; spinning only burns the pass.
                                # Stop here - resume-safe, re-run after reset.
                                LOG.warning(
                                    "daily quota exhausted on %s - stopping pass "
                                    "(resume-safe; re-run after reset)",
                                    os.path.basename(mp4),
                                )
                                return done
                            retries += 1
                            if retries > 10:
                                LOG.error(
                                    "giving up on %s after %d rate-limit retries",
                                    os.path.basename(mp4),
                                    retries,
                                )
                                break
                            LOG.warning(
                                "rate limited; sleeping %ss: %s", BACKOFF_S, exc
                            )
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
        "--config",
        default="/opt/truesight_autopilot/farm_media_daemon/media_archive_daemon_config.yaml",
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
    p_vis = sub.add_parser(
        "visual",
        help="LLM vision-caption archived clips still missing a visual_description",
    )
    p_vis.add_argument("--limit", type=int, default=0, help="max clips (0 = all)")
    p_vis.add_argument(
        "--dry-run", action="store_true", help="list what would be described"
    )
    p_vis.add_argument(
        "--archive",
        default="",
        help="only <zip>.archive.json whose name contains this (e.g. ilheus_warehouse)",
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
    gcfg = cfg.get("geo") or {}
    if gcfg.get("cache_path"):
        os.environ["FARM_MEDIA_GEO_CACHE"] = gcfg["cache_path"]
    if gcfg.get("api_key_env"):
        _load_geo_key(gcfg["api_key_env"])
    if args.action == "visual":
        run_visual(
            cfg,
            cap.get("vision_model", ""),
            args.limit,
            args.dry_run,
            getattr(args, "archive", "") or "",
        )
    elif args.action == "enrich":
        run_enrich(cfg, model, lang)
    else:
        run_backfill(cfg, args.token, model, lang)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
