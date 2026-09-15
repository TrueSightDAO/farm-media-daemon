"""Regression test: YouTube's rolling-window upload cap must be detected as a
quota error so the daemon backs off instead of hot-looping every 60s."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from farm_media_daemon import is_quota_error  # noqa: E402


def test_upload_limit_exceeded_is_quota():
    tail = (
        '<HttpError 400 when requesting None returned "The user has exceeded the '
        'number of videos they may upload."> {"reason": "uploadLimitExceeded"}'
    )
    assert is_quota_error(tail) is True


def test_reason_string_alone_is_quota():
    assert is_quota_error("uploadLimitExceeded") is True


def test_exceeded_phrase_alone_is_quota():
    assert is_quota_error("The user has exceeded the number of videos") is True


def test_classic_429_is_quota():
    assert is_quota_error("HttpError 429 rateLimitExceeded") is True
    assert is_quota_error("quota exceeded for this project") is True


def test_generic_failure_is_not_quota():
    assert is_quota_error("TIMEOUT") is False
    assert is_quota_error("HttpError 500 internal error") is False
    assert is_quota_error("") is False
