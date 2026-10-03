#!/usr/bin/env python3
"""Backfill per-item ``source_zip`` provenance into inbox sidecars.

Background
----------
Media archived *before* the "per-item source_zip" change (farm-media-daemon
PR #35) has no ``source_zip`` in its inbox sidecar, so manifests built from
those sidecars cannot trace a file back to the zip it arrived in.

The authoritative origin already exists: the ``<zip>.archive.json`` sidecar
written to ``/media/processed/`` at archive time records
``{"zip": name, "entries": {"<basename>": {"farm_id", "sha256", ...}}}``.

This tool joins the two and stamps ``source_zip`` onto each inbox sidecar so
future manifest builds (and any Sophia/LLM reading the manifest) can trace and
retrieve a file by its origin archive name.

Join key
--------
``farm_id`` + ``sha256`` (primary), falling back to ``farm_id`` + basename stem
(archive keys keep the original extension -- ``IMG_1.MOV`` -- while the inbox
file is the transcoded ``IMG_1.mp4``; size-dedup archive entries carry no
sha256, only ``exists``/``raw_url``). A key that maps to more than one zip is
ambiguous and is skipped, never guessed.

Idempotent: an existing ``source_zip`` is never overwritten. Dry-run unless
``--apply``.

Usage
-----
    python3 farm_media_backfill_source_zip.py            # report only
    python3 farm_media_backfill_source_zip.py --apply    # write sidecars
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

DEFAULT_PROCESSED = "/media/processed"
DEFAULT_INBOX = "/media/media_archive_inbox"


def _stem(name: str) -> str:
    return os.path.splitext(name)[0]


def _farm_from_raw_url(url: str) -> str | None:
    if "/raw/" not in url:
        return None
    tail = url.split("/raw/", 1)[1]
    parts = tail.split("/")
    return parts[0] if len(parts) >= 2 else None


def build_zip_index(processed_dir: str = DEFAULT_PROCESSED) -> dict:
    """(farm_id, sha256) -> zip  and  (farm_id, stem) -> zip.

    Returns (by_sha, by_stem, ambiguous_keys, n_zips).
    """
    by_sha: dict[tuple[str, str], str] = {}
    by_stem: dict[tuple[str, str], str] = {}
    ambiguous: set[tuple[str, str]] = set()
    n_zips = 0

    for path in sorted(glob.glob(os.path.join(processed_dir, "*.archive.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        zip_name = doc.get("zip") or os.path.basename(path).replace(".archive.json", "")
        entries = doc.get("entries") or {}
        if not entries:
            continue
        n_zips += 1
        for basename, e in entries.items():
            if not isinstance(e, dict):
                continue
            farm = e.get("farm_id") or _farm_from_raw_url(e.get("raw_url", "") or "")
            if not farm:
                continue
            sha = e.get("sha256")
            if sha:
                k = (farm, sha)
                if k in by_sha and by_sha[k] != zip_name:
                    ambiguous.add(k)
                else:
                    by_sha.setdefault(k, zip_name)
            ks = (farm, _stem(basename))
            if ks in by_stem and by_stem[ks] != zip_name:
                ambiguous.add(ks)
            else:
                by_stem.setdefault(ks, zip_name)

    for k in ambiguous:
        by_sha.pop(k, None)
        by_stem.pop(k, None)
    return by_sha, by_stem, ambiguous, n_zips


def backfill(
    inbox_dir: str = DEFAULT_INBOX,
    processed_dir: str = DEFAULT_PROCESSED,
    apply: bool = False,
    out=sys.stdout,
) -> dict:
    by_sha, by_stem, ambiguous, n_zips = build_zip_index(processed_dir)
    stats = {
        "zips": n_zips,
        "sidecars": 0,
        "stamped": 0,
        "already": 0,
        "unmatched": 0,
        "ambiguous_skipped": 0,
        "errors": 0,
    }

    for coll_dir in sorted(glob.glob(os.path.join(inbox_dir, "*", "*"))):
        if not os.path.isdir(coll_dir):
            continue
        farm_id = os.path.basename(coll_dir)
        for fn in sorted(os.listdir(coll_dir)):
            if not fn.endswith(".json") or fn.endswith(".raw.json"):
                continue
            sp = os.path.join(coll_dir, fn)
            try:
                with open(sp, encoding="utf-8") as fh:
                    sc = json.load(fh)
            except (OSError, ValueError):
                stats["errors"] += 1
                continue
            stats["sidecars"] += 1
            if sc.get("source_zip"):
                stats["already"] += 1
                continue
            sha = sc.get("sha256")
            base = sc.get("file") or fn[:-5]
            zip_name = None
            if sha and (farm_id, sha) in by_sha:
                zip_name = by_sha[(farm_id, sha)]
            else:
                ks = (farm_id, _stem(base))
                if ks in ambiguous:
                    stats["ambiguous_skipped"] += 1
                    print(f"  ambiguous, skipped: {farm_id}/{base}", file=out)
                    continue
                zip_name = by_stem.get(ks)
            if not zip_name:
                stats["unmatched"] += 1
                continue
            sc["source_zip"] = zip_name
            if apply:
                tmp = sp + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(sc, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, sp)
            stats["stamped"] += 1

    print(
        f"zips indexed: {stats['zips']}   ambiguous keys dropped: {len(ambiguous)}",
        file=out,
    )
    print(f"sidecars scanned: {stats['sidecars']}", file=out)
    print(f"  already have source_zip : {stats['already']}", file=out)
    print(
        f"  stamped (matched)       : {stats['stamped']}"
        f"{'' if apply else '  [dry-run]'}",
        file=out,
    )
    print(f"  unmatched (no origin)   : {stats['unmatched']}", file=out)
    print(f"  ambiguous, skipped      : {stats['ambiguous_skipped']}", file=out)
    print(f"  read errors             : {stats['errors']}", file=out)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--inbox", default=DEFAULT_INBOX)
    ap.add_argument("--processed", default=DEFAULT_PROCESSED)
    ap.add_argument(
        "--apply", action="store_true", help="write the sidecars (default: report only)"
    )
    a = ap.parse_args(argv)
    backfill(a.inbox, a.processed, apply=a.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
