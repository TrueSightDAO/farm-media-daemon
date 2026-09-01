#!/usr/bin/env python3
"""farm-media-manifest - aggregate sidecars into FARM_MEDIA_MANIFESTS/<farm>.json.

The commit step stays deliberate: a Sophia (or the governor) runs this, reviews,
then pushes via the normal PR flow. The daemon itself never touches GitHub.
"""

import argparse
import datetime
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Commit farm-media queue state to a manifest"
    )
    ap.add_argument("--inbox", default="/home/ubuntu/farm_media_inbox")
    ap.add_argument("--outdir", default="/tmp/farm_media_manifests_out")
    ap.add_argument("farm_id")
    args = ap.parse_args()

    path = os.path.join(args.inbox, args.farm_id)
    if not os.path.isdir(path):
        print(f"no inbox for {args.farm_id}", file=sys.stderr)
        return 1
    items = []
    for name in sorted(os.listdir(path)):
        if not name.endswith(".mp4"):
            continue
        sc = os.path.join(path, name + ".json")
        if not os.path.exists(sc):
            items.append({"file": name, "yt_id": None, "error": "no sidecar"})
            continue
        side = json.load(open(sc, encoding="utf-8"))
        items.append(
            {
                k: side.get(k)
                for k in (
                    "file",
                    "farm_id",
                    "sha256",
                    "gps",
                    "duration_s",
                    "yt_id",
                    "error",
                    "produced_by",
                    "generated",
                )
            }
        )
    manifest = {
        "farm_id": args.farm_id,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "item_count": len(items),
        "uploaded_count": sum(1 for i in items if i.get("yt_id")),
        "items": items,
    }
    os.makedirs(args.outdir, exist_ok=True)
    out = os.path.join(args.outdir, f"{args.farm_id}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    print(f"wrote {out} ({len(items)} items, {manifest['uploaded_count']} uploaded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
