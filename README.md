# Media Archive Daemon

> Repo kept as **`farm-media-daemon`**; this is the media archive (MAP) daemon for TrueSightDAO farm media.

One service, **two background workers**:

1. **YouTube worker** (`farm_media_daemon.py`) — transcoded videos + sidecars land in the inbox queue; the worker uploads to YouTube and writes the `yt_id` back.
2. **S3 archive worker** (`farm_media_archive.py`) — raw originals + preview frames → `media.agroverse.shop` (`raw/<farm>/` + hot `previews/<farm>/`); computes sha256, reads `captured_at` from the original, writes a resume-safe `<file>.raw.json` marker.

It **never touches GitHub** — manifests are committed deliberately by a Sophia or the CLI.

**Design:** see [DESIGN.md](DESIGN.md) (governor-approved). **Plan:** `plans/FARM_MEDIA_DAEMON_PLAN.md` in agentic_ai_context.

## How farms register
1. Process media per-farm (transcode MOV→MP4, GPS re-inject, optional YOLO detect) — stays outside this repo.
2. Drop `<file>.mp4` + `<file>.mp4.json` (sidecar, see DESIGN.md schema) into `media_archive_inbox/farm-media/<farm_id>/`.
3. Add the farm to `config.yaml` (`inboxes:`).

## How videos land & upload (YouTube worker)
- The daemon watches inboxes, uploads sidecar-complete videos, writes `yt_id` into the sidecar.
- Quota: global `daily_budget` (default 6/day), round-robin fairness, 429 backoff. Resume-safe by construction (sidecar is the state).

## How raws archive to S3 (S3 archive worker)
- Configure `archive.roots` in `config.yaml`: each root points at a farm's raw originals (an extracted dir, or a zip root once zip-streaming lands).
- Per original file the worker does one pass: **sha256** (dedupe/integrity) → **`captured_at`** read from the original MOV/HEIC (ffmpeg drops it, so it is read upstream, never inferred) → one **ffmpeg preview frame** → raw upload to `raw/<farm>/` → preview upload to hot `previews/<farm>/` → write `<file>.raw.json` marker (resume-safe; restart skips done files).
- Previews stay hot (Standard, no lifecycle) for fast explorer/timeline rendering; raws transition STANDARD_IA → DEEP_ARCHIVE per bucket lifecycle.
- The worker **never touches GitHub and never deletes originals** — pruning happens deliberately, only after manifests carry the S3 URLs.

## How manifests commit
- `farm-media-manifest commit <farm_id>` aggregates sidecars → `farm_media_manifests/<farm>.json` + opens a PR (repo TrueSightDAO/farm_media_manifests).
- Any Sophia can do this; the daemon never commits.

## CLI
- `farm-media-queue list [--farm <id>]` — uploaded / pending / needs_metadata / error.
- `farm-media-manifest commit <farm_id>` — commit step.
- `farm_media_archive.py --once` — run one archive pass (S3 worker) for testing.
- `farm-media-daemon` — run the daemon (systemd unit provided).

## Systemd
- `systemd/farm-media-daemon.service` — YouTube worker.
- `systemd/farm-media-archive.service` — S3 archive worker (sources `/opt/truesight_autopilot/.env` for AWS creds).

## Credentials
NEVER commit `config/youtube/*.json` or AWS keys — they live only on the box (gitignored / `.env`). The repo is public by design.
