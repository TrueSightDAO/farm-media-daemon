#!/usr/bin/env python3
"""farm-media-queue - read-only status of the farm-media inboxes."""

import argparse
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="List farm-media queue status")
    ap.add_argument("--inbox", default="/home/ubuntu/media_archive_inbox/farm-media")
    ap.add_argument("--farm", default=None)
    ap.add_argument("--with-yt", action="store_true", help="only items with yt_id")
    args = ap.parse_args()

    if args.farm:
        farms = [args.farm]
    else:
        farms = sorted(
            d
            for d in os.listdir(args.inbox)
            if os.path.isdir(os.path.join(args.inbox, d))
        )
    total = {"uploaded": 0, "pending": 0, "needs_metadata": 0, "error": 0}
    for farm in farms:
        path = os.path.join(args.inbox, farm)
        rows = []
        for name in sorted(os.listdir(path)):
            if not name.endswith(".mp4"):
                continue
            sc = os.path.join(path, name + ".json")
            st = {"file": name, "yt_id": None, "status": "pending", "error": None}
            if os.path.exists(sc):
                try:
                    side = json.load(open(sc, encoding="utf-8"))
                    st["yt_id"] = side.get("yt_id")
                    if side.get("error"):
                        if "needs_metadata" in side["error"]:
                            st["status"] = "needs_metadata"
                        else:
                            st["status"] = "error"
                        st["error"] = side["error"][:80]
                    elif side.get("yt_id"):
                        st["status"] = "uploaded"
                except json.JSONDecodeError:
                    st["status"] = "error"
                    st["error"] = "unparseable sidecar"
            if args.with_yt and not st["yt_id"]:
                continue
            rows.append(st)
            total[st["status"]] += 1
        print(f"\n== {farm} ==")
        for r in rows:
            print(
                f"  {r['status']:<14} {r['file']:<20} "
                f"yt={r['yt_id'] or '-':<14} {r['error'] or ''}"
            )
    print(f"\nTOTAL: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
