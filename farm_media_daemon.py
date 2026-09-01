#!/usr/bin/env python3
"""Farm Media Daemon — shared YouTube uploader for the Media Archives Pipeline (MAP).

Watches inboxes under the media archive, uploads videos whose sidecars lack a
`yt_id`, and writes the YouTube ID back into the sidecar (atomic). The daemon
never touches GitHub; committing manifests is a separate deliberate step.

Quota model: the configured daily_budget is a SOFT ceiling (set high — 429 is
the real signal). On 429/quota-exhausted the daemon pauses with escalating
backoff and retries, because YouTube's limit resets on a rolling window rather
than strictly every 24h. Only after the backoff exceeds 6h does it fall back to
sleeping until the 07:05 UTC reset boundary.
"""

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import time

import yaml

LOG = logging.getLogger("farm_media_daemon")

UPLOAD_TIMEOUT_S = 600
BACKOFF_ERROR_S = 60
QUOTA_RESET = dt.time(hour=7, minute=5)  # UTC, YouTube daily quota reset
QUOTA_BACKOFF_START_S = 15 * 60  # first pause on 429
QUOTA_BACKOFF_MAX_S = 2 * 60 * 60  # cap the 429 backoff at 2h
QUOTA_SLEEP_FALLBACK_S = 6 * 60 * 60  # after this much 429 backoff, sleep to reset


def write_sidecar(path: str, sidecar: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)  # atomic


def successes_since_reset(logpath: str, reset: dt.time) -> int:
    """Count SUCCESSFUL uploads since the last quota-reset boundary.

    Used to report pace and to apply the soft budget ceiling. Only successful
    inserts consume quota; 429-rejected attempts do not.
    """
    if not os.path.exists(logpath):
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    boundary = now.replace(
        hour=reset.hour, minute=reset.minute, second=0, microsecond=0
    )
    if boundary > now:
        boundary -= dt.timedelta(days=1)
    n = 0
    with open(logpath, encoding="utf-8") as fh:
        for line in fh:
            try:
                lt = dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=dt.timezone.utc
                )
            except ValueError:
                continue
            if lt >= boundary and "FAILED" not in line:
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


def upload_one(
    upload_cmd: list[str], mp4: str, sidecar: dict
) -> tuple[str | None, str]:
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
        tail = out[-2000:]  # keep enough to see past google-api FutureWarnings
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


def iter_sidecars(inbox_path: str):
    """Yield (mp4_path, sidecar_path, sidecar_dict) for videos without yt_id."""
    if not os.path.isdir(inbox_path):
        return
    for mp4 in sorted(os.listdir(inbox_path)):
        if not mp4.lower().endswith((".mp4", ".mov", ".m4v")):
            continue
        sc = os.path.join(inbox_path, mp4 + ".json")
        if not os.path.exists(sc):
            LOG.warning("missing sidecar for %s", mp4)
            continue
        with open(sc, encoding="utf-8") as fh:
            sidecar = json.load(fh)
        yield os.path.join(inbox_path, mp4), sc, sidecar


def missing_fields(sidecar: dict) -> list[str]:
    """Return required fields missing from a sidecar."""
    required = ["title", "description", "farm_id", "file"]
    return [f for f in required if not sidecar.get(f)]


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def run(cfg: dict, upload_cmd: list[str], logpath: str, once: bool = False) -> None:
    budget = int(cfg.get("daily_budget", 6))
    inboxes = cfg.get("inboxes", [])
    quota_backoff_s = QUOTA_BACKOFF_START_S
    while True:
        made_progress = False
        for inbox in inboxes:
            farm_id = inbox.get("farm_id")
            limit = int(inbox.get("priority", 1))
            processed = 0
            for mp4, sc, sidecar in iter_sidecars(inbox.get("path", "")):
                if sidecar.get("yt_id"):
                    continue
                missing = missing_fields(sidecar)
                if missing:
                    sidecar["error"] = f"needs_metadata: missing {','.join(missing)}"
                    write_sidecar(sc, sidecar)
                    LOG.warning("%s: %s", farm_id, sidecar["error"])
                    continue
                if processed >= limit:
                    break
                # soft ceiling: don't exceed the budget within one reset window
                if successes_since_reset(logpath, QUOTA_RESET) >= budget:
                    LOG.info("budget %d reached; pausing", budget)
                    time.sleep(60)
                    break
                processed += 1
                sidecar.setdefault("farm_id", farm_id)
                vid, tail = upload_one(upload_cmd, mp4, sidecar)
                log_attempt(logpath, farm_id, sidecar["file"], vid, 0 if vid else None)
                if vid:
                    sidecar["yt_id"] = vid
                    sidecar["error"] = None
                    write_sidecar(sc, sidecar)
                    LOG.info("%s %s -> %s", farm_id, sidecar["file"], vid)
                    made_progress = True
                    quota_backoff_s = QUOTA_BACKOFF_START_S  # reset on success
                else:
                    low = tail.lower()
                    if "quota" in low or "429" in low or "ratelimitexceeded" in low:
                        LOG.warning(
                            "%s quota exhausted; pause %.0fs then retry: %s",
                            sidecar["file"],
                            quota_backoff_s,
                            tail[-120:],
                        )
                        if once:
                            return
                        time.sleep(quota_backoff_s)
                        quota_backoff_s = min(quota_backoff_s * 2, QUOTA_BACKOFF_MAX_S)
                        if quota_backoff_s >= QUOTA_SLEEP_FALLBACK_S:
                            LOG.warning(
                                "429 persisting past %.0fs; sleeping to reset",
                                quota_backoff_s,
                            )
                            sleep_until_quota_reset()
                            quota_backoff_s = QUOTA_BACKOFF_START_S
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
    cfg = load_config(args.config)
    upload_cmd = [
        args.venv_python,
        args.upload_script,
    ]
    try:
        run(cfg, upload_cmd, args.log_file, once=args.once)
    except KeyboardInterrupt:
        LOG.info("interrupt; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
