# Media Archive Daemon — Design

**Status:** Approved (Gary Teh, 2026-09-01) · **Plan:** `plans/FARM_MEDIA_DAEMON_PLAN.md` (agentic_ai_context)

## 1. Problem

Every farm's media (photos + videos) needs to reach YouTube, the raw S3 archive, and the shared manifests. Ad-hoc per-farm uploader scripts (Cleide's throttled uploader, La do Sitio's retry loop) each reinvent quota handling, don't coordinate, and can exhaust the shared YouTube API daily quota. There is no single shared state any Sophia (or the governor) can query — and raw originals live only on a single box.

## 2. Principles (governor-approved)

1. **Metadata travels with the file.** A `<file>.json` sidecar sits next to each video carrying everything the upstream pipeline already computed (sha256, GPS, objects, duration, title, description, farm_id, provenance). The daemon never regenerates, looks up, or infers.
2. **The queue IS the inbox.** `media_archive_inbox/<source>/<farm_id>/` — pending = no `yt_id`, done = `yt_id` present, failed = `error` field. Source namespaces match MAP terminology (farm-media = first, event-media future).
3. **The daemon never touches GitHub.** It only reads sidecars, uploads, writes state back into sidecars/markers, and moves on.
4. **GitHub is the committed state.** `farm_media_manifests/<farm>.json` + `index.json` in the dedicated repo TrueSightDAO/farm_media_manifests are the durable record any Sophia reads. Committing is a deliberate step (Sophia or `manifest-commit` CLI) — never automatic per-video.
5. **Any Sophia can read/commit.** The manifests are the index; querying is just reading them. Midstream handoff between Sophias works from any thread.
6. **The governor can query any Sophia.** "Find me cacao-processing videos from Cleide" — answered from manifests, across photos + videos.
7. **Provenance in every sidecar.** `produced_by`, `generated` timestamps — so stale/wrong metadata is attributable.
8. **Capture time, raw blob, and preview are first-class.** `captured_at` (ISO-8601, harvested from the original MOV/HEIC at intake — ffmpeg drops it, so capture it BEFORE transcode), `raw_url` (stable pointer to the raw original in S3), and `preview` (frame JPG, hot in S3). The manifest is the full index: what / where / when / preview.

## 3. Layout

```
media_archive_inbox/<source>/<farm_id>/        # YouTube worker queue
  IMG_4859.mp4
  IMG_4859.mp4.json        # sidecar

<raw root>/<farm>/                             # S3 archive worker source
  IMG_4859.MOV
  IMG_4859.MOV.raw.json   # marker: sha256, captured_at, raw_url, preview_url
```

### Sidecar schema (YouTube worker)

```json
{
  "file": "IMG_4859.MOV",
  "farm_id": "cleide",
  "sha256": "...",
  "gps": "-3.4146, -52.6285",
  "objects": ["person", "cacao_pods"],
  "duration_s": 34.2,
  "captured_at": "2024-07-02T14:22:11-03:00",
  "raw_url": "https://s3.us-east-1.amazonaws.com/media.agroverse.shop/raw/cleide/IMG_4859.MOV",
  "preview": "previews/cleide/IMG_4859.jpg",
  "title": "Fazenda Cleide — IMG_4859 (cacao)",
  "description": "Cacao farm visit, Cleide & Marcelo, CEPOTX, Para, Brazil.",
  "tags": ["cacao", "agroverse", "para"],
  "privacy": "public",
  "produced_by": "sophia",
  "generated": "2026-09-01T00:00:00Z",
  "yt_id": null,
  "error": null
}
```

### Marker schema (S3 archive worker)

```json
{
  "file": "IMG_4859.MOV",
  "farm_id": "cleide",
  "sha256": "...",
  "size": 12345678,
  "captured_at": "2024-07-02T14:22:11-03:00",
  "duration_s": 34.2,
  "raw_url": "https://s3.us-east-1.amazonaws.com/media.agroverse.shop/raw/cleide/IMG_4859.MOV",
  "preview_url": "https://s3.us-east-1.amazonaws.com/media.agroverse.shop/previews/cleide/IMG_4859.jpg",
  "produced_by": "farm-media-archive",
  "uploaded_at": "2026-09-05T00:00:00Z"
}
```

## 4. Daemon loop

### YouTube worker (`farm_media_daemon.py`)

```
while True:
  for each farm inbox:
    for each mp4 with sidecar:
      if sidecar.yt_id: continue
      if sidecar incomplete: mark needs_metadata, continue
      if today's uploads >= daily_budget: sleep till reset (~07:00 UTC); continue
      upload -> write yt_id back -> log (ts, file, yt_id, rc)
      on 429: clear yt_id, backoff 600s
      on other error: sidecar.error = msg; continue (don't wedge queue)
```

### S3 archive worker (`farm_media_archive.py`)

```
while True:
  for each configured root (farm raw dir or zip):
    for each original missing a .raw.json marker:
      sha256 -> captured_at (from original) -> preview frame (ffmpeg)
      upload raw -> raw/<farm>/ ; upload preview -> previews/<farm>/
      write .raw.json marker (sha256, captured_at, raw_url, preview_url)
      on error: log, backoff, keep marker absent so file retries
  if no work: sleep
```

- **Singleton**: PID lockfile per worker; only ONE instance of each worker may write sidecars/markers. Systemd units on the autopilot box.
- **Fairness**: round-robin across farms by default; per-farm priority multiplier in config (`priority: 2` = 2:1 share).
- **Quota (YouTube)**: global daily budget (default 6/day on unverified YouTube project; configurable; multi-project support planned — `projects:` list).
- **Never deletes originals**: the S3 worker only writes markers; pruning is a deliberate step after manifests carry the S3 URLs.

## 5. CLIs (in this repo)

- `farm-media-queue list [--farm <id>] [--with yt_id]` — status: uploaded / pending / needs_metadata / error.
- `farm-media-manifest commit <farm_id>` — aggregate sidecars → `farm_media_manifests/<farm>.json` and open a PR (repo TrueSightDAO/farm_media_manifests).
- `farm_media_archive.py --once` — run one S3-archive pass (testing).
- `farm-media-daemon` — the daemon itself.

## 6. Query patterns (governor → any Sophia)

- "What's the state of Cleide's media?" → queue list + manifest.
- "Find me fermentation-barrel videos" → manifests with objects/titles.
- "All assets for Fazenda Cleide — photos + videos" → farm-media-raw (photos) + manifest (videos) + S3 (raw videos/previews).

## 7. Non-goals

- No transcode/GPS/detect in the daemon (those stay per-farm; memory-heavy).
- No automatic GitHub commits.
- No deletion of originals without a committed manifest + governor approval.
- No private data in the repo (public by design; creds live in `config/youtube/*.json` + `.env`, gitignored).
