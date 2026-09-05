#!/usr/bin/env python3
"""Farm Media Archive Worker — raw originals -> S3 `media.agroverse.shop`.

Companion service to the farm-media-daemon (YouTube uploader). Watches raw
roots and, for each raw original not yet archived, does the whole archive pass
in one shot:

  1. sha256 of the original (dedupe + integrity anchor for the manifest)
  2. captured_at from the ORIGINAL file (QuickTime/EXIF) — ffmpeg drops this
     during transcode, so it must be read here, upstream of any derivative
  3. one preview frame (ffmpeg, small JPG) — lands HOT in S3
  4. upload the raw  -> s3://<bucket>/raw/<farm>/<file>
  5. upload preview -> s3://<bucket>/previews/<farm>/<basename>.jpg
  6. write <file>.raw.json next to the original (raw_url, preview_url,
     captured_at, sha256, size, uploaded_at) — resume-safe state

The worker never touches GitHub and never deletes originals (pruning is a
separate deliberate step once manifests are committed). Raws follow the bucket
lifecycle (STANDARD_IA @30d -> DEEP_ARCHIVE @180d); previews/ has NO lifecycle
rule so previews stay hot and explorers render instantly.
"""

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import zipfile

import yaml

LOG = logging.getLogger("farm_media_archive")

S3_ENDPOINT = "https://s3.us-east-1.amazonaws.com"
EXTOOLS = ("MediaCreateDate", "CreateDate", "CreationDate", "DateTimeOriginal")
DEFAULT_EXTENSIONS = (".MOV", ".mov")
BACKOFF_ERROR_S = 60
IDLE_S = 30

try:
    import boto3
except ImportError:  # pragma: no cover - degraded mode for --check
    boto3 = None


def write_sidecar(path: str, sidecar: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)  # atomic


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_capture_time(path: str) -> str | None:
    """Best-effort ISO-8601 capture time from the ORIGINAL file.

    ffmpeg drops QuickTime GPS/dates during transcode, so this must be read
    from the original (MOV/HEIC) before any derivative exists. exiftool tags:
    MediaCreateDate (Apple QuickTime), CreateDate, DateTimeOriginal.
    """
    try:
        out = subprocess.run(
            ["exiftool", "-s", "-s", "-s"] + [f"-{t}" for t in EXTOOLS] + [path],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # exiftool -> "2024:06:25 18:27:09" (or already ISO with offset)
        if ":" in line[:5] and len(line) >= 19:
            iso = line[:19].replace(":", "-", 2).replace(" ", "T")
            rest = line[19:].strip()
            if rest.startswith(("+", "-")) and len(rest) >= 6:
                iso += rest[:6]
            return iso
    return None


def probe_duration_s(path: str) -> float | None:
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return float(out.stdout.strip()) if out.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def make_preview(src: str, dst_jpg: str, at_s: float) -> bool:
    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                f"{at_s:.1f}",
                "-i",
                src,
                "-frames:v",
                "1",
                "-q:v",
                "3",
                dst_jpg,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        return r.returncode == 0 and os.path.exists(dst_jpg)
    except (OSError, subprocess.TimeoutExpired):
        return False


def iter_raws(root: str, extensions: tuple) -> list:
    """Yield (src, marker) for originals missing a <file>.raw.json marker."""
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        if not name.lower().endswith(extensions):
            continue
        src = os.path.join(root, name)
        marker = src + ".raw.json"
        if os.path.exists(marker):
            continue  # already archived (resume-safe)
        out.append((src, marker))
    return out


def archive_one(
    s3,
    bucket: str,
    farm_id: str,
    src: str,
    marker: str | None,
    preview_frame_frac: float,
    preview_dir: str | None = None,
    as_name: str | None = None,
) -> dict:
    basename = as_name or os.path.basename(src)
    stem, _ext = os.path.splitext(basename)
    raw_key = f"raw/{farm_id}/{basename}"
    prev_key = f"previews/{farm_id}/{stem}.jpg"
    size = os.path.getsize(src)
    dig = sha256_of(src)
    captured = read_capture_time(src)
    dur = probe_duration_s(src)
    at_s = (dur * preview_frame_frac) if dur else 1.0
    prev_local = os.path.join(
        preview_dir or os.path.dirname(src), stem + ".preview.jpg"
    )
    ok = make_preview(src, prev_local, at_s)
    # raw upload (boto3 multipart auto for >8MB)
    s3.upload_file(src, bucket, raw_key)
    if ok:
        s3.upload_file(prev_local, bucket, prev_key)
        os.remove(prev_local)  # preview is derived; never keep on disk
    sidecar = {
        "file": basename,
        "farm_id": farm_id,
        "sha256": dig,
        "size": size,
        "captured_at": captured,
        "duration_s": round(dur, 2) if dur else None,
        "raw_url": f"{S3_ENDPOINT}/{bucket}/{raw_key}",
        "preview_url": f"{S3_ENDPOINT}/{bucket}/{prev_key}" if ok else None,
        "preview": ok,
        "produced_by": "farm-media-archive",
        "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if marker:
        write_sidecar(marker, sidecar)
    return sidecar


def _is_junk_entry(name: str) -> bool:
    """True for __MACOSX/ and ._ AppleDouble entries — never archived."""
    return any(
        p.startswith("__MACOSX") or p.startswith("._") for p in name.split("/") if p
    )


def iter_zip_entries(zip_path: str, extensions: tuple, archived: set) -> list:
    """Media entry names inside a zip not yet archived (junk skipped)."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        LOG.error("bad zip: %s", zip_path)
        return []
    out = []
    for info in infos:
        bn = os.path.basename(info.filename)
        if _is_junk_entry(info.filename) or not bn.lower().endswith(extensions):
            continue
        if bn in archived:
            continue
        out.append(info.filename)
    return out


def extract_zip_entry(zip_path: str, entry: str, tmpdir: str | None = None) -> str:
    """Stream ONE zip entry to a temp file — never extract the whole zip."""
    suffix = os.path.splitext(os.path.basename(entry))[1]
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="zarc_", dir=tmpdir)
    os.close(fd)
    with zipfile.ZipFile(zip_path) as zf, zf.open(entry) as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    return tmp


def load_zip_state(zip_path: str) -> tuple:
    st_path = zip_path + ".archive.json"
    if os.path.exists(st_path):
        try:
            with open(st_path, encoding="utf-8") as fh:
                return json.load(fh), st_path
        except (OSError, ValueError):
            pass
    return {"zip": os.path.basename(zip_path), "entries": {}}, st_path


def handle_zip_root(
    s3, bucket: str, farm_id: str, zip_path: str, exts: tuple, frac: float
) -> bool:
    """Archive every pending media entry of a zip individually (never the zip blob)."""
    state, st_path = load_zip_state(zip_path)
    entries = iter_zip_entries(zip_path, exts, set(state["entries"]))
    made = False
    for entry in entries:
        bn = os.path.basename(entry)
        raw_key = f"raw/{farm_id}/{bn}"
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zsize = zf.getinfo(entry).file_size
        except (zipfile.BadZipFile, KeyError):
            continue
        # size-dedupe vs S3: same farm_id + basename + byte size = already archived
        try:
            head = s3.head_object(Bucket=bucket, Key=raw_key)
            if int(head.get("ContentLength", -1)) == zsize:
                LOG.info("%s %s already in S3 (size match); skip", farm_id, bn)
                state["entries"][bn] = {
                    "exists": True,
                    "raw_url": f"{S3_ENDPOINT}/{bucket}/{raw_key}",
                }
                write_sidecar(st_path, state)
                made = True
                continue
        except Exception:  # noqa: BLE001 - 404/NoSuchKey => upload below
            pass
        tmp = extract_zip_entry(zip_path, entry, tmpdir="/tmp")
        try:
            sc = archive_one(
                s3, bucket, farm_id, tmp, None, frac, preview_dir="/tmp", as_name=bn
            )
            state["entries"][bn] = sc
            write_sidecar(st_path, state)
            LOG.info("%s %s -> raw + preview (sha %s)", farm_id, bn, sc["sha256"][:12])
            made = True
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return made


def s3_client(cfg: dict):
    region = cfg.get("archive", {}).get("region", "us-east-1")
    kid = os.environ.get("AWS_ACCESS_KEY_ID_NELANCO")
    sk = os.environ.get("AWS_SECRET_ACCESS_KEY_NELANCO")
    if kid and sk:
        return boto3.client(
            "s3", region_name=region, aws_access_key_id=kid, aws_secret_access_key=sk
        )
    return boto3.client("s3", region_name=region)  # instance/chain creds


def run(cfg: dict, once: bool = False) -> None:
    arc = cfg.get("archive") or {}
    bucket = arc.get("bucket", "media.agroverse.shop")
    frac = float(arc.get("preview_frame_frac", 0.25))
    roots = arc.get("roots") or []
    if not roots:
        LOG.info("no archive.roots configured; idle")
        return
    s3 = s3_client(cfg)
    while True:
        made = False
        for root in roots:
            farm_id = root.get("farm_id", "?")
            exts = tuple(root.get("extensions") or list(DEFAULT_EXTENSIONS))
            zip_path = root.get("zip")
            if zip_path:
                try:
                    if handle_zip_root(s3, bucket, farm_id, zip_path, exts, frac):
                        made = True
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    LOG.error("%s zip %s failed: %s", farm_id, zip_path, exc)
                    time.sleep(BACKOFF_ERROR_S)
                continue
            for src, marker in iter_raws(root.get("path", ""), exts):
                try:
                    sc = archive_one(s3, bucket, farm_id, src, marker, frac)
                    LOG.info(
                        "%s %s -> raw + preview (sha %s)",
                        farm_id,
                        os.path.basename(src),
                        sc["sha256"][:12],
                    )
                    made = True
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    LOG.error("%s %s failed: %s", farm_id, os.path.basename(src), exc)
                    time.sleep(BACKOFF_ERROR_S)
        if once:
            return
        if not made:
            time.sleep(IDLE_S)


def main() -> int:
    ap = argparse.ArgumentParser(description="Farm Media Archive Worker (S3)")
    ap.add_argument(
        "--config",
        default="/opt/truesight_autopilot/media_archive_daemon_config.yaml",
    )
    ap.add_argument("--log-file", default="/tmp/farm_media_archive.log")
    ap.add_argument("--once", action="store_true", help="single pass and exit")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.log_file:
        fh = logging.FileHandler(args.log_file)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    try:
        run(cfg, once=args.once)
    except KeyboardInterrupt:
        LOG.info("interrupt; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
