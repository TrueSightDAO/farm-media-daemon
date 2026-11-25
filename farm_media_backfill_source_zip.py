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

``farm_id`` disagrees between the two sides for some farms: the inbox directory
is named after the delivery (``raimundo-geniza-para``) while the archive sidecar
records the canonical farm (``raimundo-geniza``). ``FARM_ALIASES`` reconciles
those explicitly; anything not listed is treated as a mismatch and left
unmatched -- mis-filing is irreversible, so fail-closed.

Archive sidecars also accumulate in several roots, not just ``/media/processed``
(legacy strays in ``/media/upload_zips`` and ``$HOME``), so the index reads all
of ``DEFAULT_PROCESSED_DIRS``.

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

# Archive sidecars live in several places on the box: the canonical
# /media/processed plus legacy strays left behind in /media/upload_zips and $HOME.
DEFAULT_PROCESSED_DIRS = ("/media/processed", "/media/upload_zips", "/home/ubuntu")
DEFAULT_INBOX = "/media/media_archive_inbox"

# inbox folder name -> archive farm_id, ONLY where both name the SAME farm.
#
# Kept explicit and deliberately tiny. A suffix-strip heuristic ("-para",
# "-bahia") resolves just 1 of the 10 mismatched inbox folders and silently
# mis-files the rest -- and a wrong farm_id is an irreversible S3 mis-file.
FARM_ALIASES: dict[str, str] = {
    "raimundo-geniza-para": "raimundo-geniza",
}

# Mismatched inbox folders that MUST NOT be aliased, with the reason, so a
# future session does not "helpfully" add them (thread 30550 §0).
UNALIASED_BY_DESIGN: dict[str, str] = {
    "la-do-sitio": "a DISTINCT farm from paulo-la-do-sitio; aliasing would mis-file",
    "rancho-maranta-para": "two archive farms (rancho-maranta-plot-1 / -plot-2); not separable",
    "crf-anapu-para": "no archive sidecar exists yet",
    "cristo-rei-pacaje-para": "no archive sidecar exists yet",
    "facility-coopercabruca": "no archive sidecar exists yet",
    "jedielcio": "no archive sidecar exists yet",
    "sitio-catarinense-para": "no archive sidecar exists yet",
    "sitio-torres-pacaja-para": "no archive sidecar exists yet",
    "tutorial-media": "no archive sidecar exists yet",
}


def _stem(name: str) -> str:
    return os.path.splitext(name)[0]


def _farm_from_raw_url(url: str) -> str | None:
    if "/raw/" not in url:
        return None
    tail = url.split("/raw/", 1)[1]
    parts = tail.split("/")
    return parts[0] if len(parts) >= 2 else None


def resolve_farm_ids(inbox_farm: str) -> list[str]:
    """Archive ``farm_id``s to try for an inbox folder, alias first."""
    alias = FARM_ALIASES.get(inbox_farm)
    return [alias, inbox_farm] if alias else [inbox_farm]


def _archive_sidecars(processed_dirs) -> list[str]:
    if isinstance(processed_dirs, str):
        processed_dirs = (processed_dirs,)
    paths: list[str] = []
    for d in processed_dirs:
        paths.extend(sorted(glob.glob(os.path.join(d, "*.archive.json"))))
    return paths


def build_zip_index(processed_dirs=DEFAULT_PROCESSED_DIRS) -> dict:
    """(farm_id, sha256) -> zip  and  (farm_id, stem) -> zip, across all roots.

    Returns (by_sha, by_stem, ambiguous_keys, n_zips).
    """
    by_sha: dict[tuple[str, str], str] = {}
    by_stem: dict[tuple[str, str], str] = {}
    ambiguous: set[tuple[str, str]] = set()
    n_zips = 0

    for path in _archive_sidecars(processed_dirs):
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
    processed_dirs=DEFAULT_PROCESSED_DIRS,
    apply: bool = False,
    out=sys.stdout,
) -> dict:
    by_sha, by_stem, ambiguous, n_zips = build_zip_index(processed_dirs)
    stats = {
        "zips": n_zips,
        "sidecars": 0,
        "stamped": 0,
        "alias_resolved": 0,
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
            candidates = resolve_farm_ids(farm_id)
            zip_name = None
            matched_farm = farm_id

            if sha:
                for f in candidates:
                    z = by_sha.get((f, sha))
                    if z:
                        zip_name, matched_farm = z, f
                        break

            if not zip_name:
                hits: dict[str, str] = {}
                saw_ambiguous = False
                stem = _stem(base)
                for f in candidates:
                    k = (f, stem)
                    if k in ambiguous:
                        saw_ambiguous = True
                    z = by_stem.get(k)
                    if z:
                        hits[z] = f
                if saw_ambiguous or len(hits) > 1:
                    stats["ambiguous_skipped"] += 1
                    print(f"  ambiguous, skipped: {farm_id}/{base}", file=out)
                    continue
                if hits:
                    zip_name, matched_farm = next(iter(hits.items()))

            if not zip_name:
                stats["unmatched"] += 1
                continue
            if matched_farm != farm_id:
                stats["alias_resolved"] += 1
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
    print(f"    of which via farm alias: {stats['alias_resolved']}", file=out)
    print(f"  unmatched (no origin)   : {stats['unmatched']}", file=out)
    print(f"  ambiguous, skipped      : {stats['ambiguous_skipped']}", file=out)
    print(f"  read errors             : {stats['errors']}", file=out)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--inbox", default=DEFAULT_INBOX)
    ap.add_argument(
        "--processed",
        action="append",
        default=None,
        help=(
            "archive-sidecar root; repeatable. "
            f"default: {' '.join(DEFAULT_PROCESSED_DIRS)}"
        ),
    )
    ap.add_argument(
        "--apply", action="store_true", help="write the sidecars (default: report only)"
    )
    a = ap.parse_args(argv)
    dirs = tuple(a.processed) if a.processed else DEFAULT_PROCESSED_DIRS
    backfill(a.inbox, dirs, apply=a.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
