#!/usr/bin/env python3
"""Farm Media Photo Enrich -- OCR + vision tags for still photos (MAP).

Per governor directive (2026-09-09): photos entering the media pipeline are
archived as content-less blobs -- they get EXIF/sha/raw/preview but no content
extraction, so a photo of a Portuguese presentation slide is invisible to any
English search. Videos already get pt->en transcription (farm_media_captions.py);
this module gives still photos the same searchability so their manifest records
become English-retrievable.

For each still photo (HEIC/JPG/PNG) it adds, on top of the archive sidecar:

  ocr_text_pt   Portuguese text read off the image (tesseract, lang 'por')
  ocr_text_en   English rendering of legible text/content (Grok vision)
  scene         short English scene label, e.g. "presentation slide"
  caption_en    one English sentence describing the photo
  objects[]     salient nouns detected in the scene

Design notes
------------
* Mirrors farm_media_captions.py: this enricher is deliberately separate from
  the archive daemon (which stays dumb -- DESIGN.md principles 1 & 7). It
  ENRICHES sidecars/items in place; the daemon just archives whatever fields
  are present.
* Heavy deps (pillow_heif, PIL, httpx, boto3) are lazy -- the module imports
  standalone anywhere and degrades when tesseract or GROK_API_KEY is absent.
* Resume-safe: an item that already has `ocr_text_pt` is skipped, so re-runs
  and interrupted backfills are cheap.
* Degrade-safe: enrichment NEVER blocks archiving. Any failure logs a warning
  and returns {} so the caller (farm_media_archive.archive_one) proceeds.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import json
import logging
import os
import shutil
import subprocess
import tempfile

import yaml

LOG = logging.getLogger("farm_media_photo_enrich")

PHOTO_EXTS = (".heic", ".heif", ".jpg", ".jpeg", ".png")
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = "grok-4-1-fast-non-reasoning"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODEL = "gemini-3-flash-preview"  # project key: 2.5.x is 404 for new users; 3.x works
OCR_LANG = "por"
TESS_PSM = "3"
MAX_SIDE = 1600  # downscale large originals before the vision call
JPEG_QUALITY = 82


# --------------------------------------------------------------------------- pure helpers
def _is_photo(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in PHOTO_EXTS


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)  # atomic


def _decode_to_jpeg(src: str, dst_jpeg: str) -> bool:
    """Decode a HEIC (or any PIL-readable image) to a temp JPEG for OCR/vision."""
    try:
        from PIL import Image  # lazy

        try:
            import pillow_heif

            pillow_heif.register_heif_opener()  # HEIF must be registered or Image.open fails
        except Exception:  # pragma: no cover - HEIC without the plugin
            pass
        with Image.open(src) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = min(1.0, MAX_SIDE / max(w, h))
            if scale < 1.0:
                im = im.resize((int(w * scale), int(h * scale)))
            im.save(dst_jpeg, "JPEG", quality=JPEG_QUALITY)
        return os.path.exists(dst_jpeg)
    except Exception as exc:  # pragma: no cover - best effort
        LOG.warning("image decode failed for %s: %s", src, exc)
        return False


def _ocr_pt(jpeg: str) -> str:
    """Tesseract OCR in Portuguese. Returns '' when tesseract is absent."""
    if shutil.which("tesseract") is None:
        return ""
    try:
        r = subprocess.run(
            ["tesseract", jpeg, "stdout", "-l", OCR_LANG, "--psm", TESS_PSM],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if r.returncode != 0:
            LOG.warning("tesseract rc=%s: %s", r.returncode, r.stderr.strip()[:300])
            return ""
        return " ".join(ln.strip() for ln in r.stdout.splitlines() if ln.strip()).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("tesseract failed: %s", exc)
        return ""


def _vision(jpeg: str) -> dict:
    """Vision pass -> ocr_text_en/scene/caption_en/objects[].

    Provider: Grok by default (reliable + fast under bulk backfill). Gemini is
    measurably better at rendering foreign-language slide text into English
    (A/B on real Medicilandia photos 2026-09-09) but its image API on the
    project key is rate-limited (intermittent 403), so it is the QUALITY
    fallback and one env var away: FARM_MEDIA_VISION_PROVIDER=gemini.
    Env-configurable: FARM_MEDIA_VISION_PROVIDER=gemini|grok,
    FARM_MEDIA_GEMINI_MODEL / FARM_MEDIA_GROK_MODEL.

    Returns {} when no key is set or the call fails, so enrichment degrades
    to OCR-only rather than ever blocking the archive pass.
    """
    prompt = (
        "You are analyzing a photo from a cacao-farming convention in Para/"
        "Bahia, Brazil. Respond with STRICT JSON only, no prose:"
        '{"ocr_text_en": "<English rendering of any legible text/slide/banner '
        'content; empty string if none>", "scene": "<short English scene label, '
        'e.g. presentation slide, field demo, farmers in warehouse>", '
        '"caption_en": "<one English sentence describing the photo>", '
        '"objects": ["<salient noun>", ...]}'
    )
    try:
        with open(jpeg, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
    except OSError as exc:
        LOG.warning("vision read failed: %s", exc)
        return {}
    provider = os.environ.get("FARM_MEDIA_VISION_PROVIDER", "grok").lower()
    # Grok first: reliable + fast under bulk backfill; Gemini (better foreign-
    # text translation) is the quality fallback and stays one env var away.
    if provider == "gemini":
        out = _vision_gemini(prompt, b64)
        return out or _vision_grok(prompt, b64)  # gemini fallback -> grok
    out = _vision_grok(prompt, b64)
    if out:
        return out
    return _vision_gemini(prompt, b64)  # grok fallback -> gemini


def _call_llm_json(url, headers, payload, tag: str) -> dict:
    """POST once and parse STRICT-JSON reply into the 4 fields. Returns {} on any failure."""
    try:
        import httpx  # lazy

        resp = httpx.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        content = resp.json()
        if tag == "gemini":
            txt = content["candidates"][0]["content"]["parts"][0]["text"]
        else:
            txt = content["choices"][0]["message"]["content"]
        txt = txt.strip()
        if txt.startswith("```"):  # strip markdown fences if wrapped
            txt = txt.split("\n", 1)[-1].rsplit("```", 1)[0]
        out = json.loads(txt)
        return {
            "ocr_text_en": out.get("ocr_text_en", ""),
            "scene": out.get("scene", ""),
            "caption_en": out.get("caption_en", ""),
            "objects": out.get("objects") or [],
        }
    except Exception as exc:  # noqa: BLE001 - vision must never block archive
        LOG.warning("%s vision failed: %s", tag, exc)
        return {}


def _vision_gemini(prompt: str, b64: str) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {}
    model = os.environ.get("FARM_MEDIA_GEMINI_MODEL", GEMINI_MODEL)
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                ]
            }
        ]
    }
    return _call_llm_json(
        f"{GEMINI_ENDPOINT}/{model}:generateContent",
        {},
        body,
        "gemini",
    )


def _vision_grok(prompt: str, b64: str) -> dict:
    api_key = os.environ.get("GROK_API_KEY")
    if not api_key:
        return {}
    model = os.environ.get("FARM_MEDIA_GROK_MODEL", GROK_MODEL)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 400,
        "temperature": 0.1,
    }
    return _call_llm_json(GROK_ENDPOINT, {"Authorization": f"Bearer {api_key}"}, payload, "grok")


# --------------------------------------------------------------------------- enrich
def enrich_photo(src: str, sidecar: dict | None = None) -> dict:
    """OCR + vision-tag one still photo. Returns {} when nothing to add or when
    the sidecar is already enriched (resume-safe). Never raises."""
    if not _is_photo(os.path.basename(src)):
        return {}
    if sidecar and sidecar.get("ocr_text_pt") is not None:
        return {}  # already enriched
    jpeg = None
    try:
        fd, jpeg = tempfile.mkstemp(suffix=".jpg", prefix="fpe_")
        os.close(fd)
        if not _decode_to_jpeg(src, jpeg):
            return {}
        fields: dict = {"ocr_text_pt": _ocr_pt(jpeg)}
        fields.update(_vision(jpeg))
        if not fields.get("ocr_text_pt") and not fields.get("caption_en"):
            return {}  # nothing useful; do not mark enriched
        fields["produced_by"] = "farm-media-photo-enrich"
        fields["enriched_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        return fields
    finally:
        if jpeg:
            try:
                os.remove(jpeg)
            except OSError:
                pass


def _iter_root_sidecars(roots: list):
    for root in roots:
        path = root.get("path", "")
        if not os.path.isdir(path):
            continue
        exts = tuple(str(e).lower() for e in (root.get("extensions") or []))
        for name in sorted(os.listdir(path)):
            if not _is_photo(name):
                continue
            if exts and not name.lower().endswith(exts):
                continue
            marker = os.path.join(path, name) + ".raw.json"
            if not os.path.exists(marker):
                continue
            with open(marker, encoding="utf-8") as fh:
                sidecar = json.load(fh)
            yield os.path.join(path, name), marker, sidecar


def run_enrich(cfg: dict) -> int:
    """Enrich every still photo under archive.roots missing ocr_text_pt."""
    arc = cfg.get("archive") or {}
    changed = 0
    for src, marker, sidecar in _iter_root_sidecars(arc.get("roots") or []):
        try:
            add = enrich_photo(src, sidecar)
            if add:
                sidecar.update(add)
                _write_json(marker, sidecar)
                changed += 1
                LOG.info("enriched %s", os.path.basename(src))
        except Exception as exc:  # keep the pass going
            LOG.error("enrich failed for %s: %s", os.path.basename(src), exc)
    return changed


def run_backfill(manifest_dir: str, limit: int = 0) -> int:
    """Enrich EXISTING committed photos: walk farm_media_manifests/*.json
    items[], download each photo missing ocr_text_pt from S3 by raw_url,
    enrich, rewrite the manifest. Resume-safe + bounded (limit for test runs)."""
    import boto3  # lazy

    bucket = "media.agroverse.shop"
    # Nelanco-prefixed creds are what the archive daemon uses (archive.py
    # s3_client); bare client() would raise NoCredentialsError in that env.
    kid = os.environ.get("AWS_ACCESS_KEY_ID_NELANCO")
    sk = os.environ.get("AWS_SECRET_ACCESS_KEY_NELANCO")
    if kid and sk:
        s3 = boto3.client(
            "s3", region_name="us-east-1", aws_access_key_id=kid, aws_secret_access_key=sk
        )
    else:
        s3 = boto3.client("s3", region_name="us-east-1")  # chain/instance creds
    done = 0
    for fn in sorted(glob.glob(os.path.join(manifest_dir, "*.json"))):
        if os.path.basename(fn) == "index.json":
            continue
        try:
            with open(fn, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            LOG.error("skip %s: %s", fn, exc)
            continue
        items = data.get("items", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        touched = False
        for item in items:
            if not isinstance(item, dict) or not _is_photo(str(item.get("file", ""))):
                continue
            if item.get("ocr_text_pt") is not None:
                continue  # already enriched
            raw_url = item.get("raw_url", "")
            if not raw_url:
                continue
            try:
                key = raw_url.split("media.agroverse.shop/", 1)[-1]
                fd, tmp = tempfile.mkstemp(
                    suffix=os.path.splitext(str(item.get("file", "")))[1] or ".jpg"
                )
                os.close(fd)
                try:
                    s3.download_file(bucket, key, tmp)
                    add = enrich_photo(tmp, item)
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                if add:
                    item.update(add)
                    touched = True
                    done += 1
                    LOG.info("backfilled %s in %s", item.get("file"), os.path.basename(fn))
                    if limit and done >= limit:
                        if touched:
                            _write_json(fn, data)
                        return done
            except Exception as exc:
                LOG.error("backfill failed for %s: %s", item.get("file"), exc)
        if touched:
            _write_json(fn, data)
    return done


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Farm Media Photo Enrich (OCR + vision)")
    ap.add_argument(
        "--config",
        default="/opt/truesight_autopilot/media_archive_daemon_config.yaml",
    )
    ap.add_argument("--log-file", default="/tmp/farm_media_photo_enrich.log")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("enrich", help="enrich still photos under archive.roots")
    p.add_argument("--limit", type=int, default=0, help="max photos (0 = all)")
    b = sub.add_parser("backfill", help="enrich photos in committed manifests")
    b.add_argument("manifest_dir", help="path to a farm_media_manifests checkout")
    b.add_argument("--limit", type=int, default=0, help="max photos (0 = all)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(args.log_file), logging.StreamHandler()],
    )
    if args.action == "backfill":
        run_backfill(args.manifest_dir, args.limit)
    else:
        cfg = load_config(args.config)
        run_enrich(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
