#!/usr/bin/env python3
"""farm-media-manifest - aggregate sidecars into farm_media_manifests/<farm>.json
(repo TrueSightDAO/farm_media_manifests).

The commit step stays deliberate: a Sophia (or the governor) runs this, reviews,
then pushes via the normal PR flow. The daemon itself never touches GitHub.

Schema follows the committed farm_media_manifests repo: top-level
farm_id/plots/source_zips/generated/processed_by/counts/gps_coverage/items,
items with numeric latitude/longitude (parsed from the sidecar's gps string),
gps_raw, basename/ext, sha256, duration_s, objects, yt_id, uploaded_at, error.
"""

import argparse
import datetime
import json
import os
import sys
from collections import Counter


def _parse_gps(gps):
    """Parse a sidecar gps string ('-3.4146, -52.6285') into (lat, lon, raw)."""
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Commit farm-media queue state to a manifest"
    )
    ap.add_argument("--inbox", default="/home/ubuntu/media_archive_inbox/farm-media")
    ap.add_argument("--outdir", default="/tmp/farm_media_manifests_out")
    ap.add_argument("farm_id")
    args = ap.parse_args()

    path = os.path.join(args.inbox, args.farm_id)
    if not os.path.isdir(path):
        print(f"no inbox for {args.farm_id}", file=sys.stderr)
        return 1

    items = []
    exts = Counter()
    gps_count = 0
    MEDIA_EXTS = {".mov", ".mp4", ".m4v", ".heic", ".heif", ".jpg", ".jpeg", ".png"}
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
        side = json.load(open(sc, encoding="utf-8"))
        file_ = side.get("file") or name
        lat, lon, gps_raw = _parse_gps(side.get("gps"))
        ext = _ext(file_)
        exts[ext] += 1
        if lat is not None and lon is not None:
            gps_count += 1
        items.append(
            {
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
                "yt_id": side.get("yt_id"),
                "uploaded_at": side.get("uploaded_at"),
                "error": side.get("error"),
            }
        )

    manifest = {
        "farm_id": args.farm_id,
        "plots": [],
        "source_zips": [],
        "generated": datetime.date.today().isoformat(),
        "processed_by": "MEDIA_ARCHIVE_PIPELINE.md",
        "counts": dict(exts),
        "gps_coverage": f"{gps_count}/{len(items)} files with GPS",
        "items": items,
    }
    os.makedirs(args.outdir, exist_ok=True)
    out = os.path.join(args.outdir, f"{args.farm_id}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    print(
        f"wrote {out} ({len(items)} items, "
        f"{sum(1 for i in items if i.get('yt_id'))} uploaded, "
        f"GPS {gps_count}/{len(items)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
