# Farm Media Daemon

Shared upload daemon for TrueSightDAO farm media. Videos + metadata (sidecars) land in a queue; the daemon uploads them to YouTube and writes the `yt_id` back. It **never touches GitHub** — manifests are committed deliberately by a Sophia or the CLI.

**Design:** see [DESIGN.md](DESIGN.md) (governor-approved). **Plan:** `plans/FARM_MEDIA_DAEMON_PLAN.md` in agentic_ai_context.

## How farms register
1. Process media per-farm (transcode MOV→MP4, GPS re-inject, optional YOLO detect) — stays outside this repo.
2. Drop `<file>.mp4` + `<file>.mp4.json` (sidecar, see DESIGN.md schema) into `media_archive_inbox/farm-media/<farm_id>/`.
3. Add the farm to `config.yaml` (`inboxes:`).

## How videos land & upload
- The daemon watches inboxes, uploads sidecar-complete videos, writes `yt_id` into the sidecar.
- Quota: global `daily_budget` (default 6/day), round-robin fairness, 429 backoff. Resume-safe by construction (sidecar is the state).

## How manifests commit
- `farm-media-manifest commit <farm_id>` aggregates sidecars → `farm_media_manifests/<farm>.json` + opens a PR (repo TrueSightDAO/farm_media_manifests).
- Any Sophia can do this; the daemon never commits.

## CLI
- `farm-media-queue list [--farm <id>]` — uploaded / pending / needs_metadata / error.
- `farm-media-manifest commit <farm_id>` — commit step.
- `farm-media-daemon` — run the daemon (systemd unit provided).

## Credentials
NEVER commit `config/youtube/*.json` — they live only on the box (gitignored). The repo is public by design.
