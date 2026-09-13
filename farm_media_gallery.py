#!/usr/bin/env python3
"""farm-media-gallery -- derive the site gallery block (media.json) from MAP.

Part of MAP (see MEDIA_ARCHIVE_PIPELINE.md). The web gallery
``farms/<slug>/media.json`` was the ONLY hand-authored link in the media
pipeline, so it silently lagged: 33 Cacau na Veia clips were uploaded to
YouTube (their sidecars carried ``yt_id``) while the page stayed photos-only.
This module DERIVES the gallery instead, so "uploaded but not published" is
impossible by construction.

Design
------
* **Pure + deterministic.** ``build_gallery()`` is a side-effect-free transform
  from a list of MAP items (sidecar- or manifest-shaped) to a ``media.json``
  document. Idempotent: the same inputs always produce the same output.
* **Stable order** by capture time (``captured_at``), filename as tie-break, so
  a run that adds one clip never reshuffles the rest.
* **Only PUBLISHED videos appear**: an item with no ``yt_id`` is skipped (still
  pending upload) -- exactly the drift this module exists to prevent.
* **QA guard**: Whisper's stock hallucinations ("Legendas pela comunidade de
  Amara.org", "Subtitles by ...") are stripped; a transcript that is *entirely*
  hallucination falls back to the boilerplate intro instead of emitting garbage.
* **ffprobe** supplies ``aspect`` (portrait/landscape/square) for YouTube
  entries; if ffprobe is unavailable the field is omitted (never blocks a build).

The daemon never writes the site repo; a Sophia runs this and commits the data.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

SCHEMA_VERSION = 1

VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
IMAGE_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png"}

CAPTION_MAX_CHARS = 200
DEFAULT_INTRO = "Site walk {day} {month} {year}"
DEFAULT_IMAGE_SRC = "../../assets/images/farms/{slug}-{stem}.jpg"


def site_asset_path(src: str) -> str:
    """Map a gallery entry's relative ``src`` to a site-repo-relative path.

    Entries live at ``farms/<collection>/media.json`` on the site, so their
    ``src`` starts with ``../../`` -- e.g.
    ``../../assets/images/farms/x-y.jpg``. Strip leading ``./``/``../`` segments
    so the result is the path as it appears from the site repo root
    (``assets/images/farms/x-y.jpg``), which a caller can test for membership
    against the repo's tracked blobs.
    """
    p = str(src or "")
    while p.startswith("../"):
        p = p[3:]
    while p.startswith("./"):
        p = p[2:]
    return p

DOT = " " + chr(0xB7) + " "
DASH = " " + chr(0x2014) + " "
ELLIPSIS = chr(0x2026)
PIN = chr(0x1F4CD)

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

HALLUCINATION_MARKERS = (
    "amara.org",
    "legendas pela comunidade",
    "subtitles by",
    "subtitulos por",
    "revisado por",
    "www.legendas",
)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_DUR_RE = re.compile(r"\(\s*\d+\s*s\s*\)\s*$")


def _sentences(text: str):
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def sanitize_transcript(text: str) -> str:
    """Drop Whisper's stock hallucination sentences; return the clean remainder."""
    kept = []
    for sentence in _sentences(text):
        low = sentence.lower()
        if any(marker in low for marker in HALLUCINATION_MARKERS):
            continue
        kept.append(sentence)
    return " ".join(kept).strip()


def _clip(text: str, limit: int = CAPTION_MAX_CHARS) -> str:
    """Collapse whitespace and truncate on a word boundary with an ellipsis."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + ELLIPSIS


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _description_body(desc: str) -> str:
    """Extract the transcript paragraph from a 3-block MAP description.

    Sidecar descriptions are ``<intro>\\n\\n<place line>\\n\\n<translation>``. After
    dropping the location-pin block, if two or more blocks remain the last one is
    the translation; a description that is only an intro yields "" (boilerplate).
    """
    parts = [p.strip() for p in (desc or "").split("\n\n") if p.strip()]
    parts = [p for p in parts if not p.startswith(PIN)]
    if len(parts) <= 1:
        return ""
    return parts[-1]


def extract_transcript(item: dict) -> str:
    """Preferred transcript source: ``transcript_en``, else the description body."""
    t = (item.get("transcript_en") or "").strip()
    if t:
        return t
    return _description_body(
        item.get("description") or item.get("description_original") or ""
    )


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def build_intro(captured_at, template: str = DEFAULT_INTRO) -> str:
    """Boilerplate context line, e.g. 'Site walk 9 September 2026'."""
    d = _parse_date(captured_at)
    if d is None:
        return ""
    return template.format(day=d.day, month=MONTHS[d.month - 1], year=d.year)


def classify_aspect(width: int, height: int):
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def probe_aspect(path, runner=subprocess.run):
    """Return portrait/landscape/square for a video, or None if ffprobe can't say."""
    if not path:
        return None
    try:
        out = runner(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    lines = (out.stdout or "").strip().splitlines()
    if not lines or "," not in lines[0]:
        return None
    try:
        w, h = (int(x) for x in lines[0].split(",")[:2])
    except ValueError:
        return None
    return classify_aspect(w, h)


def media_kind(name: str):
    ext = os.path.splitext(str(name))[1].lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def build_title(item: dict) -> str:
    """Sidecar title with a duration suffix, unless one is already present."""
    title = (item.get("title") or "").strip()
    if not title:
        base = item.get("basename") or item.get("file") or ""
        title = os.path.splitext(os.path.basename(base))[0]
    d = item.get("duration_s")
    if d and not _DUR_RE.search(title):
        title = f"{title} ({int(round(float(d)))} s)"
    return title


def build_caption(item: dict, intro: str = "", place: str = "", plot: str = "") -> str:
    """'<intro> . <place> . plot <id> - <transcript excerpt>' or the boilerplate head."""
    head = DOT.join(p for p in (intro, place, f"plot {plot}" if plot else "") if p)
    body = _clip(sanitize_transcript(extract_transcript(item)))
    if body:
        return f"{head}{DASH}{body}" if head else body
    return head


def _order_key(item: dict):
    cap = (item.get("captured_at") or "").strip()
    name = item.get("basename") or item.get("file") or ""
    return (1 if not cap else 0, cap, name)


def iter_sidecar_items(inbox_dir: str):
    """Read every media file+sidecar pair from an inbox dir into item dicts."""
    items = []
    if not os.path.isdir(inbox_dir):
        return items
    for name in sorted(os.listdir(inbox_dir)):
        full = os.path.join(inbox_dir, name)
        if not os.path.isfile(full) or media_kind(name) is None:
            continue
        sc = full + ".json"
        if not os.path.exists(sc):
            continue
        with open(sc, encoding="utf-8") as fh:
            side = json.load(fh)
        side.setdefault("file", name)
        side["_path"] = full
        items.append(side)
    return items


def load_manifest_items(path: str):
    """Read a farm_media_manifests JSON -> (farm_id, plots, items)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    farm_id = data.get("farm_id")
    plots = list(data.get("plots") or [])
    items = []
    for it in data.get("items", []):
        it = dict(it)
        it["_farm_id"] = farm_id
        items.append(it)
    return farm_id, plots, items


def build_gallery(
    items,
    collection: str,
    image_src_template: str = DEFAULT_IMAGE_SRC,
    aspect_probe=None,
    intro_template: str = DEFAULT_INTRO,
    place: str = "",
    plot: str = "",
    hero=None,
    image_exists=None,
):
    """Deterministically build a ``media.json`` document from MAP items.

    ``image_exists`` (optional) is a predicate over a *site-repo-relative* asset
    path (see :func:`site_asset_path`). When supplied, image items whose
    generated ``src`` is not confirmed present are dropped, so the published
    gallery can never point at a missing file.
    """
    gallery = []
    for item in sorted(items, key=_order_key):
        name = item.get("basename") or item.get("file") or ""
        kind = media_kind(name)
        if kind == "video":
            vid = (item.get("yt_id") or "").strip()
            if not vid:  # pending upload -- never publish an unpublished clip
                continue
            entry = {"type": "youtube", "videoId": vid, "title": build_title(item)}
            caption = build_caption(
                item,
                intro=build_intro(item.get("captured_at"), intro_template),
                place=place or (item.get("place_name") or ""),
                plot=plot,
            )
            if caption:
                entry["caption"] = caption
            if aspect_probe is not None:
                aspect = aspect_probe(item)
                if aspect:
                    entry["aspect"] = aspect
            gallery.append(entry)
        elif kind == "image":
            stem = os.path.splitext(os.path.basename(name))[0]
            src = image_src_template.format(slug=collection, stem=stem)
            if image_exists is not None and not image_exists(site_asset_path(src)):
                # Asset absent from the site repo -- rendering it would 404.
                continue
            gallery.append(
                {
                    "type": "image",
                    "src": src,
                    "alt": _first_line(item.get("description")) or stem,
                }
            )

    doc = {"schemaVersion": SCHEMA_VERSION}
    hero_entry = hero
    if hero_entry is None:  # default hero = the first image, if any
        for entry in gallery:
            if entry["type"] == "image":
                hero_entry = {
                    "type": "image",
                    "src": entry["src"],
                    "alt": entry["alt"],
                }
                break
    if hero_entry:
        doc["hero"] = hero_entry
    doc["gallery"] = gallery
    return doc


def _default_aspect(item):
    return probe_aspect(item.get("_path"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Derive a site gallery (media.json) block from MAP sidecars/manifest"
    )
    ap.add_argument("collection")
    ap.add_argument(
        "--inbox",
        help="inbox dir of <file>+<file>.json sidecars "
        "(default: /media/media_archive_inbox/farm-media/<collection>)",
    )
    ap.add_argument(
        "--manifest", help="read items from a farm_media_manifests JSON instead"
    )
    ap.add_argument("--outdir", default="/tmp/farm_media_galleries_out")
    ap.add_argument("--plot", default="", help="plot id, e.g. N-06-66")
    ap.add_argument("--place", default="", help="place override, e.g. 'Pacaja, Para'")
    ap.add_argument("--intro-template", default=DEFAULT_INTRO)
    ap.add_argument("--image-src-template", default=DEFAULT_IMAGE_SRC)
    ap.add_argument(
        "--no-aspect", action="store_true", help="skip the ffprobe aspect probe"
    )
    args = ap.parse_args(argv)

    if args.manifest:
        farm_id, plots, items = load_manifest_items(args.manifest)
        collection = farm_id or args.collection
        plot = args.plot or (plots[0] if plots else "")
    else:
        inbox = args.inbox or f"/media/media_archive_inbox/farm-media/{args.collection}"
        items = iter_sidecar_items(inbox)
        collection = args.collection
        plot = args.plot

    doc = build_gallery(
        items,
        collection,
        image_src_template=args.image_src_template,
        aspect_probe=None if args.no_aspect else _default_aspect,
        intro_template=args.intro_template,
        place=args.place,
        plot=plot,
    )

    out = os.path.join(args.outdir, "galleries", f"{collection}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    yt = sum(1 for e in doc["gallery"] if e["type"] == "youtube")
    skipped = sum(
        1
        for it in items
        if media_kind(it.get("basename") or it.get("file") or "") == "video"
        and not (it.get("yt_id") or "").strip()
    )
    print(
        f"wrote {out}: {yt} youtube + {len(doc['gallery']) - yt} image entries "
        f"({skipped} unpublished videos skipped)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
