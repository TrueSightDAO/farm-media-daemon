"""Unit C -- still photos must never be archived to S3 (they belong in GitHub).

Regression guard for the silent mis-route: a root with photo extensions in its
`extensions` list used to stream HEIC/JPG straight to S3 `raw/<farm>/`, and a root
with NO `extensions` key silently defaulted to .MOV/.mov without any warning.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import farm_media_archive as fma  # noqa: E402


def test_photo_extensions_are_stripped_from_video(caplog):
    root = {
        "farm_id": "sao-jorge",
        "zip": "/media/upload_zips/sao_jorge_fazenda.zip",
        "extensions": [".MOV", ".mov", ".mp4", ".HEIC", ".heic", ".JPG", ".jpg"],
    }
    with caplog.at_level(logging.WARNING):
        video, photo = fma.resolve_extensions(root, "sao-jorge")
    assert video == (".MOV", ".mov", ".mp4")
    assert set(photo) == {".HEIC", ".heic", ".JPG", ".jpg"}
    # the safety invariant: no photo extension may survive into the S3 list
    assert not [e for e in video if e.lower() in fma._PHOTO_EXTS]


def test_missing_extensions_key_is_loud(caplog):
    root = {"farm_id": "oops", "path": "/media/oops"}
    with caplog.at_level(logging.WARNING):
        video, photo = fma.resolve_extensions(root, "oops")
    assert video == fma.DEFAULT_EXTENSIONS
    assert photo == ()
    assert any("NO `extensions`" in r.getMessage() for r in caplog.records)


def test_photo_only_root_yields_no_video(caplog):
    root = {
        "farm_id": "stills",
        "path": "/media/stills",
        "extensions": [".HEIC", ".jpg"],
    }
    with caplog.at_level(logging.WARNING):
        video, photo = fma.resolve_extensions(root, "stills")
    assert video == ()
    assert set(photo) == {".HEIC", ".jpg"}


def test_iter_raws_never_picks_stills(tmp_path):
    """End-to-end: a legacy root listing photos selects only the video file."""
    (tmp_path / "clip.MOV").write_bytes(b"v")
    (tmp_path / "pic.HEIC").write_bytes(b"p")
    video, _photo = fma.resolve_extensions(
        {"farm_id": "f", "extensions": [".mov", ".HEIC"]}, "f"
    )
    picked = [os.path.basename(src) for src, _m in fma.iter_raws(str(tmp_path), video)]
    assert picked == ["clip.MOV"]
