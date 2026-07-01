"""Task 6 — R2 attachment upload (RED)."""
from __future__ import annotations

import pytest

from sidequest.server import r2_upload
from sidequest.server.r2_upload import R2UploadError, object_key, safe_filename


def test_safe_filename_sanitizes() -> None:
    assert safe_filename("my shot!.png") == "my_shot_.png"
    assert safe_filename("../../etc/passwd") == "etc_passwd"
    assert safe_filename("") == "file"


def test_object_key_layout() -> None:
    assert object_key("abc123", 2, "shot.png") == "bug-reports/abc123/2-shot.png"


def test_upload_bytes_returns_cdn_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}

    class FakeClient:
        def put_object(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "https://cdn.slabgorb.com")
    monkeypatch.setattr(r2_upload, "_build_client", lambda: FakeClient())

    url = r2_upload.upload_bytes("bug-reports/x/0-shot.png", b"data", "image/png")
    assert url == "https://cdn.slabgorb.com/bug-reports/x/0-shot.png"
    assert calls["Bucket"] == "sidequest"
    assert calls["Key"] == "bug-reports/x/0-shot.png"
    assert calls["ContentType"] == "image/png"


def test_upload_bytes_wraps_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def put_object(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(r2_upload, "_build_client", lambda: FakeClient())
    with pytest.raises(R2UploadError):
        r2_upload.upload_bytes("k", b"d", "image/png")


@pytest.mark.parametrize("override", ["", "local"])
def test_cdn_base_forces_real_cdn_in_local_mode(monkeypatch: pytest.MonkeyPatch, override: str) -> None:
    from sidequest.server.r2_upload import cdn_base

    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", override)
    assert cdn_base() == "https://cdn.slabgorb.com"


def test_object_key_rejects_traversal_report_id() -> None:
    from sidequest.server.r2_upload import object_key

    with pytest.raises(ValueError):
        object_key("../../other-prefix", 0, "shot.png")
