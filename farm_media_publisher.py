#!/usr/bin/env python3
"""farm-media-publisher -- reconcile farm media from the inbox into the
machine-owned ``farm_media_manifests`` repo.

Part of MAP (see MEDIA_ARCHIVE_PIPELINE.md / DESIGN.md). PR5 of the Media
Gallery Publisher plan (handoffs/MEDIA_GALLERY_PUBLISHER_PLAN.md).

Why
---
The web gallery ``farms/<slug>/media.json`` was the ONLY hand-authored link in
the media pipeline, so it silently lagged the daemon: 33 Cacau na Veia clips
were uploaded to YouTube (their sidecars carried ``yt_id``) while the page
stayed photos-only. This publisher is the missing reconcile -- run on a systemd
timer, it derives the gallery block from the inbox and writes
``galleries/<collection>.json`` into the machine-owned ``farm_media_manifests``
repo via the Contents API. On a timer, ``uploaded`` == ``published`` holds by
construction.

Design
------
* **Idempotent.** The output is a pure function of the inputs, so a run that
  changes nothing writes nothing (the Contents API is not even called when the
  rendered bytes match what is already committed).
* **Never clobbers.** The publisher writes ONLY ``galleries/<collection>.json``.
  It deliberately does NOT rewrite ``<farm_id>.json``: those carry hand-verified
  enrichment (``site_name``/``owner``/``plots``/``field_notes``/``transcript_en``
  /``plot_id``) that ``farm_media_manifest.build_manifest`` does not reproduce,
  so re-deriving them here would destroy data. The gallery is instead derived
  FROM the committed (rich) manifest when one exists, else from the inbox
  sidecars -- ``source`` records which.
* **Tolerates auth flakiness.** A 401/403 raises :class:`AuthError` and the run
  retries with backoff before exiting non-zero, so the timer surfaces a real
  failure instead of silently skipping.
* **One commit per collection per run.**

``captured_at`` (ordering + intro text) lives in the inbox sidecar; the
committed manifest often lacks it, so the inbox is consulted for the fields the
manifest does not carry.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import farm_media_gallery as gallery

API = "https://api.github.com"
DEFAULT_REPO = "TrueSightDAO/farm_media_manifests"
DEFAULT_INBOX = "/home/ubuntu/media_archive_inbox/farm-media"
DEFAULT_SITE_REPO = "TrueSightDAO/agroverse_shop_beta"
DEFAULT_SITE_BRANCH = "main"
GALLERY_SUBDIR = "galleries"
_USER_AGENT = "farm-media-publisher/1.0"


class AuthError(RuntimeError):
    """The token is missing/invalid (as opposed to a transient blip)."""


def _collections_from_inbox(inbox):
    """Sorted collection ids = subdirectories of the inbox."""
    if not os.path.isdir(inbox):
        return []
    return sorted(d for d in os.listdir(inbox) if os.path.isdir(os.path.join(inbox, d)))


def _first_plot(data):
    """First plot id from a manifest's ``plots`` (list of str OR list of dict)."""
    for p in data.get("plots") or []:
        value = p.get("plot_id") if isinstance(p, dict) else p
        if value:
            return str(value)
    return ""


class GitHubClient:
    """Minimal Contents-API client: read a file, create/update a file."""

    def __init__(self, token, repo=DEFAULT_REPO, api=API, timeout=30):
        if not token:
            raise AuthError("no token supplied")
        self.token = token
        self.repo = repo
        self.api = api.rstrip("/")
        self.timeout = timeout

    def _headers(self, req):
        """Attach the standard auth/accept headers to ``req``."""
        req.add_header("Authorization", "token " + self.token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", _USER_AGENT)
        return req

    def _send(self, req):
        """Send ``req``: 404 -> None, 401/403 -> AuthError, else the JSON body."""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # absent -- callers treat as "create"
            if exc.code in (401, 403):
                raise AuthError("{} on {}".format(exc.code, req.full_url)) from exc
            raise

    def _request(self, method, path, body=None):
        url = "{}/repos/{}/contents/{}".format(
            self.api, self.repo, urllib.parse.quote(path)
        )
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        self._headers(req)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return self._send(req)

    def list_tree_paths(self, branch="main"):
        """Blob paths in the repo's tree at ``branch`` (recursive).

        A missing repo/branch (404) yields an empty set; 401/403 raise
        :class:`AuthError`. Lets the publisher confirm an image asset actually
        exists in the site repo before emitting a ``src`` for it.
        """
        url = "{}/repos/{}/git/trees/{}?recursive=1".format(
            self.api, self.repo, urllib.parse.quote(branch, safe="")
        )
        req = urllib.request.Request(url, method="GET")
        self._headers(req)
        data = self._send(req)
        tree = (data or {}).get("tree") or []
        return {t["path"] for t in tree if t.get("type") == "blob"}

    def get_content(self, path):
        """Return ``(text, blob_sha)`` for a file, or ``(None, None)`` if absent."""
        resp = self._request("GET", path)
        if not resp or resp.get("type") != "file":
            return None, None
        return base64.b64decode(resp["content"]).decode("utf-8"), resp["sha"]

    def put_if_changed(self, path, text, message):
        """Create/update ``path`` only when its bytes differ.

        Returns ``"created"``, ``"updated"`` or ``"unchanged"``. Because the
        comparison happens before the PUT, an unchanged collection produces no
        commit at all -- the property the timer relies on.
        """
        existing_text, existing_sha = self.get_content(path)
        if existing_text is not None and existing_text == text:
            return "unchanged"
        body = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }
        if existing_sha:
            body["sha"] = existing_sha
        self._request("PUT", path, body)
        return "updated" if existing_sha else "created"


def _gallery_text(doc):
    """Canonical serialisation for a gallery doc (matches the site's 1-space .json)."""
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def publish_collection(
    client,
    collection,
    *,
    inbox=DEFAULT_INBOX,
    outdir=None,
    plot="",
    place="",
    aspect_probe=None,
    image_exists=None,
):
    """Reconcile one collection. Returns a result dict.

    Reads the committed manifest when present (``source == "manifest"``), else
    the inbox sidecars (``source == "sidecars"``); writes the gallery via the
    client, or into ``outdir`` when set (local/CI mode -- never used by the
    timer).
    """
    manifest_text, _ = client.get_content(collection + ".json")
    if manifest_text is None:
        # Some manifests live under farms/.
        manifest_text, _ = client.get_content("farms/" + collection + ".json")

    source = "sidecars"
    items = None
    if manifest_text:
        data = json.loads(manifest_text)
        items = [dict(it, _farm_id=collection) for it in data.get("items", [])]
        plot = plot or _first_plot(data)
        place = place or (data.get("place_name") or "")
        source = "manifest"
    if items is None:
        items = gallery.iter_sidecar_items(os.path.join(inbox, collection))

    doc = gallery.build_gallery(
        items,
        collection,
        aspect_probe=aspect_probe,
        plot=plot,
        place=place,
        image_exists=image_exists,
    )
    text = _gallery_text(doc)
    youtube = sum(1 for e in doc["gallery"] if e.get("type") == "youtube")
    rel = "{}/{}.json".format(GALLERY_SUBDIR, collection)

    if outdir is not None:
        dest = os.path.join(outdir, GALLERY_SUBDIR, collection + ".json")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        existed = os.path.exists(dest)
        prior = open(dest, encoding="utf-8").read() if existed else None
        if prior == text:
            status = "unchanged"
        else:
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(text)
            status = "updated" if existed else "created"
    else:
        status = client.put_if_changed(
            rel, text, "publish gallery: {} ({} videos)".format(collection, youtube)
        )

    return {
        "collection": collection,
        "source": source,
        "status": status,
        "youtube": youtube,
        "entries": len(doc["gallery"]),
    }


def publish_all(
    client,
    *,
    inbox=DEFAULT_INBOX,
    outdir=None,
    only=None,
    aspect_probe=None,
    sleep=0.0,
    image_exists=None,
):
    """Reconcile every inbox collection (or just ``only``). Idempotent overall."""
    collections = [only] if only else _collections_from_inbox(inbox)
    results = []
    for i, collection in enumerate(collections):
        try:
            results.append(
                publish_collection(
                    client,
                    collection,
                    inbox=inbox,
                    outdir=outdir,
                    aspect_probe=aspect_probe,
                    image_exists=image_exists,
                )
            )
        except AuthError:
            raise
        except Exception as exc:  # one bad collection must not abort the sweep
            results.append(
                {"collection": collection, "status": "error", "error": str(exc)}
            )
        if sleep and i + 1 < len(collections):
            time.sleep(sleep)
    return results


def _publish_with_retry(fn, retries, delay):
    """Run ``fn``; on AuthError retry up to ``retries`` times with ``delay`` backoff."""
    last = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except AuthError as exc:
            last = exc
            if attempt >= retries:
                break
            time.sleep(delay)
    raise last


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile inbox media -> galleries/<collection>.json in "
        "the farm_media_manifests repo (idempotent)."
    )
    ap.add_argument("--inbox", default=DEFAULT_INBOX)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument(
        "--token-env",
        default="TRUESIGHT_DAO_AUTOPILOT",
        help="env var holding the GitHub token (default: TRUESIGHT_DAO_AUTOPILOT)",
    )
    ap.add_argument("--collection", default=None, help="reconcile a single collection")
    ap.add_argument(
        "--outdir",
        default=None,
        help="local mode: write under <outdir>/galleries/ instead of the API "
        "(for tests/CI; the timer never passes this)",
    )
    ap.add_argument("--place", default="", help="place override for captions")
    ap.add_argument("--plot", default="", help="plot id override for captions")
    ap.add_argument(
        "--site-repo",
        default=DEFAULT_SITE_REPO,
        help="repo whose tracked assets gate image entries (default: %(default)s)",
    )
    ap.add_argument(
        "--site-branch",
        default=DEFAULT_SITE_BRANCH,
        help="branch of --site-repo to read assets from (default: %(default)s)",
    )
    ap.add_argument("--no-aspect", action="store_true", help="skip the ffprobe probe")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--retry-delay", type=float, default=10.0)
    args = ap.parse_args(argv)

    if args.dry_run:
        args.outdir = args.outdir or "/tmp/farm_media_publisher_dryrun"

    probe = None if args.no_aspect else gallery._default_aspect

    if args.outdir is not None:
        client = _NullClient()
        image_exists = None
    else:
        token = os.environ.get(args.token_env, "")
        if not token:
            print(
                "ERROR: env {} is empty -- set it (systemd EnvironmentFile)".format(
                    args.token_env
                ),
                file=sys.stderr,
            )
            return 1
        client = GitHubClient(token, repo=args.repo)
        site_client = GitHubClient(token, repo=args.site_repo)
        try:
            paths = _publish_with_retry(
                lambda: site_client.list_tree_paths(args.site_branch),
                args.retries,
                args.retry_delay,
            )
        except AuthError as exc:
            print(
                "ERROR: cannot read assets from {}@{}: {}".format(
                    args.site_repo, args.site_branch, exc
                ),
                file=sys.stderr,
            )
            return 1
        if not paths:
            print(
                "ERROR: no blobs in {}@{} -- refusing to publish (an empty tree "
                "would drop every image)".format(args.site_repo, args.site_branch),
                file=sys.stderr,
            )
            return 1

        def image_exists(path):
            return path in paths

    def run():
        return publish_all(
            client,
            inbox=args.inbox,
            outdir=args.outdir,
            only=args.collection,
            aspect_probe=probe,
            image_exists=image_exists,
        )

    try:
        results = _publish_with_retry(run, args.retries, args.retry_delay)
    except AuthError as exc:
        print("ERROR: auth failed after retries: {}".format(exc), file=sys.stderr)
        return 1

    failures = 0
    for r in results:
        if r.get("status") == "error":
            failures += 1
            print(
                "{}: ERROR {}".format(r["collection"], r.get("error")),
                file=sys.stderr,
            )
        else:
            print(
                "{}: {} ({} videos, {} entries) [source={}]".format(
                    r["collection"],
                    r["status"],
                    r["youtube"],
                    r["entries"],
                    r["source"],
                )
            )
    if not results:
        print("no collections found under {}".format(args.inbox))
    return 1 if failures else 0


class _NullClient:
    """A client that never touches the network -- used by ``--outdir``/``--dry-run``.

    ``get_content`` always reports the file as absent, so a local run derives
    the gallery straight from the inbox sidecars.
    """

    def get_content(self, path):  # noqa: ARG002
        return None, None

    def put_if_changed(self, path, text, message):  # noqa: ARG002
        raise RuntimeError("local mode must be run with --outdir")


if __name__ == "__main__":
    sys.exit(main())
