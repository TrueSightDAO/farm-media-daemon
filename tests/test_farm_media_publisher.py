import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_publisher as pub  # noqa: E402


class FakeClient:
    """In-memory stand-in for GitHubClient -- records path -> bytes."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.puts = []

    def get_content(self, path):
        if path in self.files:
            return self.files[path], "sha-" + path
        return None, None

    def put_if_changed(self, path, text, message):
        if path in self.files and self.files[path] == text:
            return "unchanged"
        existed = path in self.files
        self.files[path] = text
        self.puts.append((path, message))
        return "updated" if existed else "created"


def _sidecar_item(**kw):
    base = {
        "file": "A.MOV",
        "basename": "A.MOV",
        "captured_at": "2026-09-09T14:00:00",
        "yt_id": "abc",
        "title": "Farm - A",
        "duration_s": 8.0,
        "place_name": "Anapu",
    }
    base.update(kw)
    return base


def _write_inbox(tmp_path, collection, items):
    """Create <tmp_path>/<collection>/<file>+<file>.json; return the INBOX ROOT.

    ``publish_collection`` resolves ``<inbox>/<collection>``, so the helper must
    hand back the root, not the collection dir.
    """
    d = tmp_path / collection
    d.mkdir(parents=True)
    for it in items:
        name = it["basename"]
        (d / name).write_text("x")  # the media file itself
        (d / (name + ".json")).write_text(json.dumps(it))
    return str(tmp_path)


# ---------------------------------------------------------------- helpers


def test_first_plot_handles_str_and_dict():
    assert pub._first_plot({"plots": ["N-06-66"]}) == "N-06-66"
    assert pub._first_plot({"plots": [{"plot_id": "N-06-99"}]}) == "N-06-99"
    assert pub._first_plot({}) == ""


def test_collections_from_inbox(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "note.txt").write_text("x")
    assert pub._collections_from_inbox(str(tmp_path)) == ["a", "b"]


# ------------------------------------------------------- source selection


def test_source_is_manifest_when_present():
    manifest = json.dumps(
        {
            "farm_id": "f",
            "plots": ["N-06-66"],
            "items": [_sidecar_item(yt_id="m1")],
        }
    )
    client = FakeClient({"f.json": manifest})
    r = pub.publish_collection(client, "f", inbox="/nonexistent")
    assert r["source"] == "manifest"
    assert r["youtube"] == 1
    doc = json.loads(client.files["galleries/f.json"])
    assert doc["gallery"][0]["videoId"] == "m1"


def test_manifest_farms_subdir_fallback():
    manifest = json.dumps({"farm_id": "f", "items": [_sidecar_item(yt_id="z")]})
    client = FakeClient({"farms/f.json": manifest})
    r = pub.publish_collection(client, "f", inbox="/nonexistent")
    assert r["source"] == "manifest"
    assert r["youtube"] == 1


def test_source_is_sidecars_when_no_manifest(tmp_path):
    inbox = _write_inbox(tmp_path, "g", [_sidecar_item(yt_id="s1")])
    client = FakeClient()
    r = pub.publish_collection(client, "g", inbox=inbox)
    assert r["source"] == "sidecars"
    assert r["youtube"] == 1


# --------------------------------------------------------- idempotency


def test_second_run_is_unchanged(tmp_path):
    inbox = _write_inbox(tmp_path, "g", [_sidecar_item(yt_id="s1")])
    client = FakeClient()
    first = pub.publish_collection(client, "g", inbox=inbox)
    second = pub.publish_collection(client, "g", inbox=inbox)
    assert first["status"] == "created"
    assert second["status"] == "unchanged"
    assert len(client.puts) == 1  # only the first run wrote


def test_capture_time_ordering_stable(tmp_path):
    items = [
        _sidecar_item(
            file="B.MOV", basename="B.MOV", yt_id="b", captured_at="2026-09-09T15:00:00"
        ),
        _sidecar_item(
            file="A.MOV", basename="A.MOV", yt_id="a", captured_at="2026-09-09T14:00:00"
        ),
    ]
    inbox = _write_inbox(tmp_path, "g", items)
    client = FakeClient()
    pub.publish_collection(client, "g", inbox=inbox)
    doc = json.loads(client.files["galleries/g.json"])
    assert [e["videoId"] for e in doc["gallery"]] == ["a", "b"]


# --------------------------------------------------- never-clobber rule


def test_manifest_file_is_never_rewritten():
    manifest = json.dumps({"farm_id": "f", "items": [_sidecar_item(yt_id="m1")]})
    client = FakeClient({"f.json": manifest})
    pub.publish_collection(client, "f", inbox="/nonexistent")
    assert client.puts == [("galleries/f.json", client.puts[0][1])]
    assert all(p.startswith("galleries/") for p, _ in client.puts)
    assert client.files["f.json"] == manifest  # untouched byte-for-byte


# -------------------------------------------------------------- publish_all


def test_publish_all_sweeps_inbox(tmp_path):
    _write_inbox(tmp_path, "a", [_sidecar_item(yt_id="1")])
    _write_inbox(tmp_path, "b", [_sidecar_item(yt_id="2")])
    client = FakeClient()
    results = pub.publish_all(client, inbox=str(tmp_path))
    assert {r["collection"] for r in results} == {"a", "b"}
    assert all(r["status"] == "created" for r in results)


def test_publish_all_isolates_one_bad_collection(tmp_path, monkeypatch):
    _write_inbox(tmp_path, "a", [_sidecar_item(yt_id="1")])
    _write_inbox(tmp_path, "bad", [_sidecar_item(yt_id="2")])

    real = pub.publish_collection

    def boom(client, collection, **kw):
        if collection == "bad":
            raise ValueError("kaboom")
        return real(client, collection, **kw)

    monkeypatch.setattr(pub, "publish_collection", boom)
    results = pub.publish_all(FakeClient(), inbox=str(tmp_path))
    by = {r["collection"]: r for r in results}
    assert by["a"]["status"] == "created"
    assert by["bad"]["status"] == "error"


# ------------------------------------------------------------- retry logic


def test_retry_recovers_after_auth_error(monkeypatch):
    monkeypatch.setattr(pub.time, "sleep", lambda *_a: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise pub.AuthError("401")
        return ["ok"]

    assert pub._publish_with_retry(flaky, retries=3, delay=0) == ["ok"]
    assert calls["n"] == 3


def test_retry_gives_up_and_raises(monkeypatch):
    monkeypatch.setattr(pub.time, "sleep", lambda *_a: None)

    def always():
        raise pub.AuthError("403")

    try:
        pub._publish_with_retry(always, retries=1, delay=0)
    except pub.AuthError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected AuthError")


# ----------------------------------------------------------------- client


def test_client_requires_token():
    try:
        pub.GitHubClient("")
    except pub.AuthError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected AuthError")


def test_put_if_changed_skips_identical():
    client = FakeClient({"galleries/f.json": "same"})
    assert client.put_if_changed("galleries/f.json", "same", "m") == "unchanged"
    assert client.puts == []


# -------------------------------------------------------------- CLI e2e


def test_main_local_mode_creates_then_noops(tmp_path, monkeypatch):
    inbox = _write_inbox(tmp_path, "g", [_sidecar_item(yt_id="s1")])
    outdir = tmp_path / "out"
    rc = pub.main(
        ["--inbox", inbox, "--outdir", str(outdir), "--no-aspect", "--collection", "g"]
    )
    assert rc == 0
    dest = outdir / "galleries" / "g.json"
    assert dest.exists()
    before = dest.read_text()
    rc2 = pub.main(
        ["--inbox", inbox, "--outdir", str(outdir), "--no-aspect", "--collection", "g"]
    )
    assert rc2 == 0
    assert dest.read_text() == before  # idempotent on disk


def test_main_missing_token_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("TRUESIGHT_DAO_AUTOPILOT", raising=False)
    inbox = _write_inbox(tmp_path, "g", [_sidecar_item(yt_id="s1")])
    rc = pub.main(["--inbox", inbox, "--no-aspect", "--collection", "g"])
    assert rc == 1


def test_main_live_auth_failure_returns_1(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUESIGHT_DAO_AUTOPILOT", "x")

    def boom(self, *a, **k):
        raise pub.AuthError("403")

    monkeypatch.setattr(pub.GitHubClient, "get_content", boom)
    inbox = _write_inbox(tmp_path, "g", [_sidecar_item(yt_id="s1")])
    rc = pub.main(
        ["--inbox", inbox, "--no-aspect", "--collection", "g", "--retry-delay", "0"]
    )
    assert rc == 1
