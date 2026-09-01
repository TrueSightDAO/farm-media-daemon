#!/usr/bin/env python3
"""Farm Media Daemon - shared YouTube uploader for TrueSightDAO farm media.

Governor-approved design: see DESIGN.md in this repo; plan in
agentic_ai_context/plans/FARM_MEDIA_DAEMON_PLAN.md.

Responsibilities (and only these):
  1. Watch farm inboxes for mp4+sidecar pairs.
  2. Upload sidecar-complete videos to YouTube.
  3. Write yt_id back into the sidecar; log every attempt.
  4. Respect a global daily budget; back off on quota (429).
  5. NEVER touch GitHub.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time

LOG = logging.getLogger("farm_media_daemon")

QUOTA_RESET = dt.time(hour=7, minute=5)  # UTC, YouTube daily quota reset
BACKOFF_QUOTA_S = 600
BACKOFF_ERROR_S = 60
UPLOAD_TIMEOUT_S = 900
LOCKFILE = "/tmp/farm_media_daemon.pid"


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"config not found: {path}")
    with open(path, encoding="utf-8") as fh:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            return json.load(fh)
        return yaml.safe_load(fh) or {}


def acquire_lock() -> None:
    if os.path.exists(LOCKFILE):
        try:
            with open(LOCKFILE, encoding="utf-8") as fh:
                pid = int(fh.read().strip())
            os.kill(pid, 0)  # raises if dead
            raise SystemExit(f"another daemon is running (pid {pid}); refusing to start")
        except (ValueError, ProcessLookupError):
            LOG.warning("stale lockfile %s ignored", LOCKFILE)
    with open(LOCKFILE, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))


def release_lock() -> None:
    try:
        os.unlink(LOCKFILE)
    except FileNotFoundError:
        pass


def iter_sidecars(inbox_path: str):
    """Yield (mp4_path, sidecar_path, sidecar) for complete pairs."""
    if not os.path.isdir(inbox_path):
        return
    for name in sorted(os.listdir(inbox_path)):
        if not name.endswith(".mp4"):
            continue
        mp4 = os.path.join(inbox_path, name)
        sc = mp4 + ".json"
        if not os.path.exists(sc):
            LOG.warning("%s has no sidecar; skipping", mp4)
            continue
        try:
            with open(sc, encoding="utf-8") as fh:
                sidecar = json.load(fh)
        except json.JSONDecodeError:
            LOG.error("%s unparseable sidecar; skipping", sc)
            continue
        yield mp4, sc, sidecar


def missing_fields(sidecar: dict) -> list[str]:
    required = ("file", "farm_id", "title")
    return [k for k in required if not sidecar.get(k)]


def write_sidecar(path: str, sidecar: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)  # atomic


def attempts_today(logpath: str) -> int:
    if not os.path.exists(logpath):
        return 0
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    n = 0
    with open(logpath, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(today):
                n += 1
    return n


def sleep_until_quota_reset() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    nxt = now.replace(hour=QUOTA_RESET.hour, minute=QUOTA_RESET.minute, second=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    wait = (nxt - now).total_seconds()
    LOG.info("daily budget spent; sleeping %.1fh to %s UTC", wait / 3600, nxt)
    time.sleep(min(wait, 3600.0))


def upload_one(upload_cmd: list[str], mp4: str, sidecar: dict) -> tuple[str | None, str]:
    """Return (video_id, output_tail). video_id None on failure."""
    desc = sidecar.get("description") or ""
    cmd = upload_cmd + [mp4, "--title", sidecar["title"], "--description", desc]
    privacy = sidecar.get("privacy", "public")
    cmd += ["--privacy", privacy]
    tags = sidecar.get("tags") or []
    if tags:
        cmd += ["--tags", *tags]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=UPLOAD_TIMEOUT_S
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        tail = out[-300:]
        for line in out.splitlines():
            if "Video ID:" in line:
                vid = line.split("Video ID:", 1)[-1].strip()
                return (vid or None), tail
        return None, tail
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"


def log_attempt(
    logpath: str, farm_id: str, filename: str, video_id: str | None, rc: int | None
) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(logpath, "a", encoding="utf-8") as fh:
        fh.write(
            f"{ts} {farm_id} {filename}: {video_id or 'FAILED'} "
            f"rc={rc if rc is not None else 'timeout'}\n"
        )


def run(cfg: dict, upload_cmd: list[str], logpath: str, once: bool = False) -> None:
    budget = int(cfg.get("daily_budget", 6))
    inboxes = cfg.get("inboxes", [])
    while True:
        used = attempts_today(logpath)
        if used >= budget:
            sleep_until_quota_reset()
            continue
        made_progress = False
        for inbox in inboxes:
            if attempts_today(logpath) >= budget:
                break
            farm_id = inbox.get("farm_id")
            limit = int(inbox.get("priority", 1))
            processed = 0
            for mp4, sc, sidecar in iter_sidecars(inbox.get("path", "")):
                if attempts_today(logpath) >= budget:
                    break
                if sidecar.get("yt_id"):
                    continue
                missing = missing_fields(sidecar)
                if missing:
                    sidecar["error"] = (
                        f"needs_metadata: missing {','.join(missing)}"
                    )
                    write_sidecar(sc, sidecar)
                    LOG.warning("%s: %s", farm_id, sidecar["error"])
                    continue
                if processed >= limit:
                    break
                processed += 1
                sidecar.setdefault("farm_id", farm_id)
                vid, tail = upload_one(upload_cmd, mp4, sidecar)
                log_attempt(
                    logpath, farm_id, sidecar["file"], vid, 0 if vid else None
                )
                if vid:
                    sidecar["yt_id"] = vid
                    sidecar["error"] = None
                    write_sidecar(sc, sidecar)
                    LOG.info("%s %s -> %s", farm_id, sidecar["file"], vid)
                    made_progress = True
                else:
                    low = tail.lower()
                    if "quota" in low or "429" in low:
                        LOG.warning("%s quota hit: %s", sidecar["file"], tail[-120:])
                        time.sleep(BACKOFF_QUOTA_S)
                    else:
                        sidecar["error"] = tail[-200:]
                        write_sidecar(sc, sidecar)
                        LOG.error("%s failed: %s", sidecar["file"], tail[-120:])
                        time.sleep(BACKOFF_ERROR_S)
        if once:
            return
        if not made_progress:
            time.sleep(30)


def main() -> int:
    ap = argparse.ArgumentParser(description="Farm Media Daemon")
    ap.add_argument(
        "--config",
        default="/opt/truesight_autopilot/media_archive_daemon_config.yaml",
    )
    ap.add_argument(
        "--upload-script",
        default="/opt/truesight_autopilot/config/youtube/upload_video_to_youtube.py",
    )
    ap.add_argument(
        "--venv-python", default="/opt/truesight_autopilot/.venv/bin/python"
    )
    ap.add_argument("--log-file", default="/tmp/farm_media_daemon.log")
    ap.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.log_file:
        fh = logging.FileHandler(args.log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(fh)

    cfg = load_config(args.config)
    upload_cmd = [args.venv_python, args.upload_script]
    logpath = os.path.join(
        os.path.dirname(os.path.abspath(args.log_file)), "farm_media_uploads.log"
    )

    if not args.once:
        acquire_lock()
    try:
        run(cfg, upload_cmd, logpath, once=args.once)
    finally:
        if not args.once:
            release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
