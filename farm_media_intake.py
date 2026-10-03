#!/usr/bin/env python3
"""farm-media-intake -- the MAP front door: claim dropped zips safely.

Part of MAP (see MEDIA_ARCHIVE_PIPELINE.md / DESIGN.md). Unit A of the intake
design (thread 30550).

Why
---
``/media/to_process/`` is where farm zips are dropped, but until now nothing
consumed it: a dropped zip simply sat there (~9 GB / 5 zips as of 2026-09-28)
with no signal, no guard against the same zip being handled twice, and no guard
against the disk filling mid-intake.

This watcher is a thin front door. On each run it:
  1. finds *settled* ``*.zip`` files in ``to_process/`` (mtime older than
     ``settle_seconds`` -- it never claims a zip that is still uploading),
  2. **sha256-dedupes** them against a ledger of everything already claimed,
  3. checks free space and **refuses to claim** below ``min_free_gb``,
  4. atomically **claims** each new zip with ``os.rename`` into ``processing/``
     (same filesystem -> atomic, so a zip is either wholly unclaimed or wholly
     claimed, never half), and
  5. records the sha256 in the ledger, so a re-upload is recognised as a
     duplicate and moved into ``duplicates/`` rather than re-processed.

It never deletes anything and never touches the network: whatever consumes
``processing/`` (the archive / daemon workers) does the real downstream work.

Safety
------
* ``--dry-run`` prints the plan and changes nothing.
* Every filesystem action is a *move* (reversible) -- there is no ``unlink``.
* ``os.rename`` is only atomic within one filesystem, so the code asserts that
  and fails loudly instead of falling back to a copy (a half-copied 3 GB zip is
  worse than a refusal).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import time

import yaml

LOG = logging.getLogger("farm_media_intake")

DEFAULT_TO_PROCESS = "/media/to_process"
DEFAULT_PROCESSING = "/media/processing"
DEFAULT_PROCESSED = "/media/processed"
DEFAULT_DUPLICATES = "/media/duplicates"
DEFAULT_LEDGER = "/opt/truesight_autopilot/farm_media_intake_ledger.json"
DEFAULT_SETTLE_S = 300
DEFAULT_MIN_FREE_GB = 20.0
ZIP_SUFFIX = ".zip"
# Per-zip identity card: `foo.zip` + sibling `foo.zip.context.json` (thread 30550
# §4.1). The card is the ONLY safe source of `farm_id` for a dropped zip whose
# farm can't be derived -- the front door ferries it and never guesses. It is
# inert to the claimer (we enumerate *.zip only) but travels to_process -> processing.
CONTEXT_SUFFIX = ".zip.context.json"

# 2 == disk guard tripped (loud, so systemd surfaces it)
EXIT_REFUSED = 2


def sha256_of(path: str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _is_candidate(name: str) -> bool:
    """A real dropped zip -- not a dotfile, AppleDouble, or partial download."""
    if name.startswith(".") or name.startswith("._"):
        return False
    return name.lower().endswith(ZIP_SUFFIX)


def settled_candidates(to_process: str, settle_seconds: int, now: float) -> list:
    """Sorted *settled* ``*.zip`` paths in ``to_process`` (older than settle)."""
    if not os.path.isdir(to_process):
        return []
    out = []
    for name in sorted(os.listdir(to_process)):
        if not _is_candidate(name):
            continue
        path = os.path.join(to_process, name)
        if not os.path.isfile(path):
            continue
        age = now - os.path.getmtime(path)
        if age < settle_seconds:
            LOG.info(
                "skip %s: still settling (mtime %.0fs ago < %ds)",
                name,
                age,
                settle_seconds,
            )
            continue
        out.append(path)
    return out


def context_card_path(zip_path: str) -> str:
    """Sibling identity card for ``foo.zip`` -> ``foo.zip.context.json``."""
    return zip_path + ".context.json"


def read_context_card(zip_path: str):
    """Parse the sibling context card, or ``None`` when there is no card.

    A card that EXISTS but is malformed RAISES: we refuse to treat a broken card
    as "no card" (that would silently re-hide a zip the governor meant to give
    context for). Absent card -> legitimate ``None``.
    """
    path = context_card_path(zip_path)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        card = json.load(fh)
    if not isinstance(card, dict):
        raise TypeError(f"context card {path} must be a JSON object")
    return card


def _configured_zip_farm_ids(cfg: dict) -> set:
    """Every zip name the archive config already maps to a farm_id."""
    ids = set()
    for root in (cfg.get("archive") or {}).get("roots") or []:
        for k in root.get("zip_farm_ids") or {}:
            ids.add(k)
        if root.get("zip"):
            ids.add(os.path.basename(root["zip"]))
    return ids


def awaiting_context_zips(processing: str, cfg: dict) -> list:
    """Settled zips in ``processing/`` with NO context card AND not in any
    ``zip_farm_ids`` -- i.e. zips the back door will hold. The self-asking digest."""
    if not os.path.isdir(processing):
        return []
    mapped = _configured_zip_farm_ids(cfg)
    out = []
    for name in sorted(os.listdir(processing)):
        if name.startswith(".") or not name.lower().endswith(ZIP_SUFFIX):
            continue
        if name in mapped:
            continue
        if os.path.exists(os.path.join(processing, name + ".context.json")):
            continue
        out.append(name)
    return out


def load_ledger(path: str) -> dict:
    """``{"claimed": {sha256: {...}}}`` -- fresh if missing/unreadable."""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("claimed"), dict):
                return data
        except (OSError, ValueError):
            LOG.warning("ledger %s unreadable; starting fresh", path)
    return {"claimed": {}}


def save_ledger(path: str, ledger: dict) -> None:
    tmp = path + ".tmp"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)  # atomic


def _require_same_fs(src_dir: str, dst_dir: str, zip_path: str) -> None:
    """``os.rename`` is atomic only within one filesystem -- assert, never copy."""
    if os.stat(src_dir).st_dev != os.stat(dst_dir).st_dev:
        raise OSError(
            "{} and {} are on different filesystems; refusing to move {} "
            "(a copy could be half-done)".format(
                src_dir, dst_dir, os.path.basename(zip_path)
            )
        )


def _move(src: str, dst_dir: str, *, dry_run: bool) -> str:
    """Move ``src`` into ``dst_dir``; never overwrite an existing target."""
    dst = os.path.join(dst_dir, os.path.basename(src))
    if os.path.exists(dst):
        raise FileExistsError("refusing to overwrite existing {}".format(dst))
    if not dry_run:
        _require_same_fs(os.path.dirname(src), dst_dir, src)
        os.rename(src, dst)
    return dst


def _free_bytes(path: str) -> int:
    """Free bytes on the filesystem holding ``path``.

    Walks up to the nearest existing ancestor: in ``--dry-run`` (and before the
    first real run) ``processing/`` may not exist yet, and ``disk_usage`` fails
    on a missing path.
    """
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe or "/").free


def run_intake(
    cfg: dict, *, dry_run: bool = False, now=None, free_bytes_fn=None
) -> dict:
    """One intake pass. Returns a result dict (claimed / duplicate / refused)."""
    now = time.time() if now is None else now
    inc = cfg.get("intake") or {}
    to_process = inc.get("to_process", DEFAULT_TO_PROCESS)
    processing = inc.get("processing", DEFAULT_PROCESSING)
    duplicates = inc.get("duplicates", DEFAULT_DUPLICATES)
    ledger_path = inc.get("ledger", DEFAULT_LEDGER)
    settle = int(inc.get("settle_seconds", DEFAULT_SETTLE_S))
    min_free_gb = float(inc.get("min_free_gb", DEFAULT_MIN_FREE_GB))

    ledger = load_ledger(ledger_path)
    claimed = ledger["claimed"]
    result = {"claimed": [], "duplicate": [], "refused": None, "awaiting_context": []}

    candidates = settled_candidates(to_process, settle, now)
    if not candidates:
        return result

    free_fn = free_bytes_fn or _free_bytes
    if not dry_run:
        os.makedirs(processing, exist_ok=True)
        os.makedirs(duplicates, exist_ok=True)

    for src in candidates:
        name = os.path.basename(src)
        # Read the card BEFORE claiming: a broken card must never hide a zip.
        try:
            card = read_context_card(src)
            card_error = None
        except Exception as exc:  # noqa: BLE001 -- any card error must surface
            card, card_error = None, str(exc)
        digest = sha256_of(src)
        if digest in claimed:
            LOG.warning(
                "%s: duplicate of %s (sha %s) -> %s",
                name,
                claimed[digest].get("path", "?"),
                digest[:12],
                duplicates,
            )
            _move(src, duplicates, dry_run=dry_run)
            result["duplicate"].append(name)
            continue
        size = os.path.getsize(src)
        need = size + int(min_free_gb * (1024**3))
        if free_fn(processing) < need:
            LOG.error(
                "REFUSING to claim %s: free space below min_free_gb=%.0f "
                "(need %.1f GB incl. headroom)",
                name,
                min_free_gb,
                size / 1024**3,
            )
            result["refused"] = name
            break  # loud refusal; the rest waits for a future run
        dst = _move(src, processing, dry_run=dry_run)
        # ferry the identity card with its zip (inert to the claimer until now)
        card_src = context_card_path(src)
        if os.path.exists(card_src):
            _move(card_src, processing, dry_run=dry_run)
        entry = {
            "path": name,
            "size": size,
            "claimed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        if card_error:
            entry["awaiting_context"] = True
            entry["context_error"] = card_error
            result["awaiting_context"].append(f"{name} (bad card)")
        elif card is None or not card.get("farm_id"):
            entry["awaiting_context"] = True
            result["awaiting_context"].append(name)
        else:
            entry["context"] = {
                k: card[k]
                for k in ("farm_id", "title", "event_date", "location")
                if card.get(k) is not None
            }
        claimed[digest] = entry
        LOG.info(
            "%sclaimed %s -> %s (sha %s, %.2f GB)",
            "[dry-run] " if dry_run else "",
            name,
            dst,
            digest[:12],
            size / 1024**3,
        )
        result["claimed"].append(name)

    if not dry_run and (result["claimed"] or result["duplicate"]):
        save_ledger(ledger_path, ledger)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Farm Media Intake (MAP front door)")
    ap.add_argument(
        "--config",
        default="/opt/truesight_autopilot/media_archive_daemon_config.yaml",
    )
    ap.add_argument("--log-file", default="/tmp/farm_media_intake.log")
    ap.add_argument(
        "--dry-run", action="store_true", help="print the plan, change nothing"
    )
    ap.add_argument("--once", action="store_true", help="single pass (timer default)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.log_file and not args.dry_run:
        fh = logging.FileHandler(args.log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)

    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    res = run_intake(cfg, dry_run=args.dry_run)
    processing = (cfg.get("intake") or {}).get("processing", DEFAULT_PROCESSING)
    awaiting = sorted(
        set(res.get("awaiting_context") or [])
        | set(awaiting_context_zips(processing, cfg))
    )
    if awaiting:
        LOG.warning(
            "AWAITING CONTEXT: %d zip(s) held (no known farm_id): %s -- drop a "
            "<zip>.context.json beside it, or add a zip_farm_ids entry",
            len(awaiting),
            ", ".join(awaiting),
        )
    LOG.info(
        "intake: claimed=%d duplicate=%d refused=%s awaiting_context=%d",
        len(res["claimed"]),
        len(res["duplicate"]),
        res["refused"],
        len(awaiting),
    )
    return EXIT_REFUSED if res["refused"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
