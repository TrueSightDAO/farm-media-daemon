#!/usr/bin/env python3
"""farm-media-manifest - aggregate sidecars into farm_media_manifests/<farm>.json
(repo TrueSightDAO/farm_media_manifests).

The commit step stays deliberate: a Sophia (or the governor) runs this, reviews,
then pushes via the normal PR flow. The daemon itself never touches GitHub.

Schema follows the committed farm_media_manifests repo: top-level
farm_id/plots/source_zips/generated/processed_by/counts/gps_coverage/items,
items with numeric latitude/longitude (parsed from the sidecar's gps string),
gps_raw, basename/ext, sha256, duration_s, objects, description, place_name,
place_address, place_id, yt_id, uploaded_at, error.
"""

import argparse
import datetime
import json
import os
import sys
from collections import Counter

# Sentinel: build_manifest(..., locations=<default>) resolves to the cached
# location index lazily; pass an explicit LocationIndex (or None) to override.
_AUTO = object()


def _default_locations():
    """Best-effort load of the cached nearest-location index (never raises)."""
    try:
        from farm_media_locations import load_default_index

        return load_default_index()
    except Exception:
        return None


def _nearest_coverage(items):
    """'<within>/<gps>' summary of GPS items matched to a known location."""
    gps = [i for i in items if i.get("nearest_distance_m") is not None]
    within = sum(1 for i in gps if i.get("nearest_location_ok"))
    return f"{within}/{len(gps)} GPS items within 2 km of a known location"


def _parse_gps(gps):
    """Parse a sidecar gps value into (lat, lon, raw).

    Delegates to farm_media_geo.parse_gps so the manifest and the daemon share
    ONE parser. Sidecars may carry either decimal ("-3.41, -52.63") or the DMS
    form exiftool emits from Apple media ("3 deg 33' 25.20\" S, ..."). The old
    local implementation only understood decimal, so every DMS-carrying file
    (MOV/HEIC from iPhone) landed as latitude=None in the manifest.
    Falls back to a decimal-only parse if the geo module can't be imported.
    """
    try:
        from farm_media_geo import parse_gps as _geo_parse_gps

        return _geo_parse_gps(gps)
    except Exception:
        pass
    if not gps:
        return None, None, None
    try:
        parts = [p.strip() for p in str(gps).split(",")]
        if len(parts) == 2:
            return float(parts[0]), float(parts[1]), str(gps)
    except (TypeError, ValueError):
        pass
    return None, None, str(gps) if gps else None


def _ext(name):
    return os.path.splitext(name)[1].lstrip(".").upper() or "UNKNOWN"


GALLERY_SUBDIR = "galleries"
MEDIA_EXTS = {".mov", ".mp4", ".m4v", ".heic", ".heif", ".jpg", ".jpeg", ".png"}


def build_manifest(farm_id, inbox_dir, today=None, with_paths=False, locations=_AUTO):
    """Aggregate media+sidecar pairs for ``farm_id`` into a manifest dict.

    Pure (no writes). The sidecar is the single source of truth for an item's
    ``yt_id``, so re-running this against an existing manifest is exactly how a
    manifest that predates an upload gets its ``yt_id`` backfilled. ``with_paths``
    attaches a private ``_path`` per item (ffprobe aspect probe); stripped by
    :func:`strip_private` before serialisation.

    ``locations`` is an optional :class:`farm_media_locations.LocationIndex`;
    when present, every GPS-bearing item is annotated with its nearest known
    plot/tree (see :mod:`farm_media_locations`). Omitted -> the cached index is
    loaded lazily; pass ``None`` to skip the join entirely.
    """
    if locations is _AUTO:
        locations = _default_locations()
    path = os.path.join(inbox_dir, farm_id)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"no inbox for {farm_id}: {path}")

    items = []
    exts = Counter()
    gps_count = 0
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        ext = os.path.splitext(name)[1].lower()
        if not os.path.isfile(full) or ext not in MEDIA_EXTS:
            continue  # skip sidecars (.json) and non-media
        sc = full + ".json"
        if not os.path.exists(sc):
            items.append(
                {
                    "file": name,
                    "basename": name,
                    "ext": _ext(name),
                    "yt_id": None,
                    "error": "no sidecar",
                }
            )
            continue
        with open(sc, encoding="utf-8") as fh:
            side = json.load(fh)
        file_ = side.get("file") or name
        lat, lon, gps_raw = _parse_gps(side.get("gps"))
        ext = _ext(file_)
        exts[ext] += 1
        if lat is not None and lon is not None:
            gps_count += 1
        entry = {
            "file": file_,
            "basename": os.path.basename(file_),
            "ext": ext,
            "size_bytes": side.get("size_bytes"),
            "sha256": side.get("sha256"),
            "duration_s": side.get("duration_s"),
            "latitude": lat,
            "longitude": lon,
            "gps_raw": gps_raw,
            "objects": side.get("objects", []),
            "description": side.get("description"),
            "place_name": side.get("place_name"),
            "place_address": side.get("place_address"),
            "place_id": side.get("place_id"),
            "yt_id": side.get("yt_id"),
            "uploaded_at": side.get("uploaded_at"),
            "error": side.get("error"),
        }
        if with_paths:
            entry["_path"] = full
        if locations is not None and lat is not None and lon is not None:
            near = locations.nearest(lat, lon)
            if near:
                entry.update(near)
        items.append(entry)

    manifest = {
        "farm_id": farm_id,
        "plots": [],
        "source_zips": [],
        "generated": (today or datetime.date.today()).isoformat(),
        "processed_by": "MEDIA_ARCHIVE_PIPELINE.md",
        "counts": dict(exts),
        "gps_coverage": f"{gps_count}/{len(items)} files with GPS",
        "items": items,
    }
    if locations is not None:
        manifest["nearest_location_coverage"] = _nearest_coverage(items)
    return manifest


def strip_private(manifest):
    """Return a copy with private ``_``-prefixed item keys removed."""
    out = dict(manifest)
    out["items"] = [
        {k: v for k, v in item.items() if not k.startswith("_")}
        for item in manifest.get("items", [])
    ]
    return out


def gallery_from_manifest(manifest, collection=None, **kwargs):
    """Derive the site gallery block from a manifest's items.

    Thin adapter over ``farm_media_gallery.build_gallery`` -- the same derived
    artifact whether it is fed sidecars or a manifest.
    """
    import farm_media_gallery as gallery

    coll = collection or manifest.get("farm_id")
    return gallery.build_gallery(manifest.get("items", []), coll, **kwargs)


def parity(manifest, gallery_doc):
    """Three-way reconciliation of uploaded vs published clips.

    ``sidecars_with_yt_id == manifest_yt_id == gallery_youtube``. The manifest
    copies ``yt_id`` straight from the sidecars, so those two agree by
    construction; the value is the third leg -- confirming the *published*
    gallery exposes exactly the uploaded clips.
    """
    items = manifest.get("items", [])
    sidecar_yt = sum(1 for it in items if (it.get("yt_id") or "").strip())
    manifest_yt = sidecar_yt
    gallery_yt = sum(
        1 for e in gallery_doc.get("gallery", []) if e.get("type") == "youtube"
    )
    return {
        "sidecars_with_yt_id": sidecar_yt,
        "manifest_yt_id": manifest_yt,
        "gallery_youtube": gallery_yt,
        "ok": sidecar_yt == manifest_yt == gallery_yt,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Commit farm-media queue state to a manifest"
    )
    ap.add_argument("farm_id")
    ap.add_argument("--inbox", default="/home/ubuntu/media_archive_inbox/farm-media")
    ap.add_argument("--outdir", default="/tmp/farm_media_manifests_out")
    ap.add_argument(
        "--with-gallery",
        action="store_true",
        help="also emit galleries/<collection>.json (the site gallery block)",
    )
    ap.add_argument(
        "--collection", help="collection id for the gallery (default: farm_id)"
    )
    ap.add_argument("--plot", default="", help="plot id for gallery captions")
    ap.add_argument("--place", default="", help="place override for gallery captions")
    ap.add_argument(
        "--no-aspect", action="store_true", help="skip the ffprobe aspect probe"
    )
    ap.add_argument(
        "--skip-parity", action="store_true", help="do not run the parity check"
    )
    ap.add_argument(
        "--no-locations",
        action="store_true",
        help="skip the nearest-location join",
    )
    ap.add_argument(
        "--locations-cache",
        default=None,
        help="override the locations cache path",
    )
    ap.add_argument(
        "--refresh-locations",
        action="store_true",
        help="refresh the locations cache from the live geojson before building",
    )
    args = ap.parse_args(argv)

    locations = None
    if not args.no_locations:
        try:
            import farm_media_locations as loc

            cache = args.locations_cache or loc.DEFAULT_CACHE
            if args.refresh_locations:
                loc.refresh(cache)
            locations = loc.load_default_index(cache)
        except Exception:
            locations = None

    try:
        manifest = build_manifest(
            args.farm_id,
            args.inbox,
            with_paths=args.with_gallery,
            locations=locations,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    os.makedirs(args.outdir, exist_ok=True)
    out = os.path.join(args.outdir, f"{args.farm_id}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(strip_private(manifest), fh, indent=2, ensure_ascii=False)
    uploaded = sum(1 for i in manifest["items"] if i.get("yt_id"))
    print(
        f"wrote {out} ({len(manifest['items'])} items, "
        f"{uploaded} uploaded, "
        f"GPS {manifest['gps_coverage'].split(' ')[0]})"
    )

    if not args.with_gallery:
        return 0

    import farm_media_gallery as gallery

    probe = None if args.no_aspect else gallery._default_aspect
    collection = args.collection or args.farm_id
    doc = gallery_from_manifest(
        manifest,
        args.collection,
        aspect_probe=probe,
        place=args.place,
        plot=args.plot,
    )
    gout = os.path.join(args.outdir, GALLERY_SUBDIR, f"{collection}.json")
    os.makedirs(os.path.dirname(gout), exist_ok=True)
    with open(gout, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    yt = sum(1 for e in doc["gallery"] if e["type"] == "youtube")
    print(f"wrote {gout}: {yt} youtube + {len(doc['gallery']) - yt} image entries")

    if not args.skip_parity:
        p = parity(manifest, doc)
        print(
            "parity: sidecars_with_yt_id={sidecars_with_yt_id} "
            "manifest_yt_id={manifest_yt_id} gallery_youtube={gallery_youtube} "
            "ok={ok}".format(**p)
        )
        if not p["ok"]:
            print("PARITY MISMATCH", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
