from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Callable

import httpx
import pytest

from scoring_service.config import EvaluationConfig, digest
from scoring_service.documents import DocumentStore
from scoring_service.models import DocumentSpec


CUTOFF = "2025-02-01T12:00:00Z"
UPDATED = "2024-01-01T00:00:00Z"
URL = "https://docs.example.com/runbook"


def spec(**changes: object) -> DocumentSpec:
    return DocumentSpec.model_validate({"id": "doc-1", "url": URL, "step_id": "step-1", **changes})


def store(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    mode: str = "live",
    **config: object,
) -> DocumentStore:
    def no_network(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Unexpected network request: {request.method}")
    return DocumentStore(
        EvaluationConfig.model_validate({"allowed_document_hosts": ["docs.example.com"], **config}),
        tmp_path, tmp_path / "cache", mode,
        httpx.MockTransport(handler or no_network),
    )


def test_replay_local_snapshot_retains_exact_bytes_and_manifest(tmp_path: Path) -> None:
    data = b"\xef\xbb\xbf# Runbook\r\nKeep this text.\r\n"
    (tmp_path / "runbook.md").write_bytes(data)
    document = spec(snapshot_path="runbook.md", snapshot_sha256=hashlib.sha256(data).hexdigest().upper(),
                    last_updated=UPDATED, version="revision-3", historical_version_verified=True)
    evidence = store(tmp_path, mode="replay").fetch(document, CUTOFF)
    assert evidence.status == "AVAILABLE"
    assert evidence.content.encode("utf-8") == data
    assert evidence.content_sha256 == hashlib.sha256(data).hexdigest()
    assert evidence.last_updated == UPDATED
    assert evidence.version == "revision-3"
    assert evidence.historical_version_verified is True
    assert evidence.retrieved_at
    assert json.loads(evidence.metadata_provenance)["manifest"] == document.model_dump(mode="json")
    assert list((tmp_path / "cache" / "documents").glob("*.json"))


def test_unhashed_local_declarations_are_preserved_not_verified(tmp_path: Path) -> None:
    (tmp_path / "runbook.md").write_bytes(b"Runbook")
    evidence = store(tmp_path, mode="replay").fetch(
        spec(snapshot_path="runbook.md", last_updated=UPDATED, version="v1", historical_version_verified=True), CUTOFF,
    )
    assert evidence.status == "AVAILABLE"
    assert evidence.last_updated is None
    assert evidence.version is None
    assert not evidence.historical_version_verified
    assert "Unverified manifest" in evidence.reason
    assert json.loads(evidence.metadata_provenance)["manifest"]["last_updated"] == UPDATED


def test_snapshot_dates_are_left_for_parent_scoring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOC_TOKEN", raising=False)
    data = b"New revision"
    (tmp_path / "doc").write_bytes(data)
    result = store(tmp_path, mode="replay", document_auth_env="DOC_TOKEN").fetch(
        spec(snapshot_path="doc", snapshot_sha256=hashlib.sha256(data).hexdigest(),
             last_updated="2026-01-01T00:00:00Z", historical_version_verified=True), CUTOFF,
    )
    assert result.status == "AVAILABLE"
    assert result.last_updated == "2026-01-01T00:00:00Z"
    assert result.historical_version_verified


@pytest.mark.parametrize("snapshot_path", ["../outside.md", "C:\\outside.md", "/outside.md"])
def test_snapshot_traversal_is_blocked(tmp_path: Path, snapshot_path: str) -> None:
    assert store(tmp_path, mode="replay").fetch(spec(snapshot_path=snapshot_path), CUTOFF).status == "BLOCKED"


def test_missing_snapshot_is_explicit(tmp_path: Path) -> None:
    assert store(tmp_path, mode="replay").fetch(spec(snapshot_path="missing.md"), CUTOFF).status == "NOT_FOUND"


@pytest.mark.parametrize("data,status", [
    (b"123456789", "TOO_LARGE"),
    (b"\xff", "UNSUPPORTED"),
    (b"a\x00b", "UNSUPPORTED"),
    (b" \r\n", "EMPTY_CONTENT"),
])
def test_local_content_failures(tmp_path: Path, data: bytes, status: str) -> None:
    (tmp_path / "doc").write_bytes(data)
    result = store(tmp_path, mode="replay", max_document_bytes=8).fetch(spec(snapshot_path="doc"), CUTOFF)
    assert result.status == status
    assert result.content == ""


@pytest.mark.parametrize("declared", ["0" * 64, "not-a-sha256"])
def test_local_hash_validation(tmp_path: Path, declared: str) -> None:
    (tmp_path / "doc").write_bytes(b"hello")
    result = store(tmp_path, mode="replay").fetch(spec(snapshot_path="doc", snapshot_sha256=declared), CUTOFF)
    assert result.status == "INTEGRITY_ERROR"


@pytest.mark.parametrize("field,value", [
    ("cutoff", "2025-02-01T12:00:00"),
    ("cutoff", "2025-99-01T12:00:00Z"),
    ("last_updated", "yesterday"),
    ("last_updated", "2025-02-01T12:00:00+25:00"),
])
def test_malformed_timestamps_never_make_requests(tmp_path: Path, field: str, value: str) -> None:
    result = store(tmp_path).fetch(spec(**({field: value} if field != "cutoff" else {})),
                                   value if field == "cutoff" else CUTOFF)
    assert result.status == "MALFORMED_TIMESTAMP"


@pytest.mark.parametrize("url,status", [
    ("http://docs.example.com/doc", "UNSUPPORTED"),
    ("file:///etc/passwd", "BLOCKED"),
    ("javascript:alert(1)", "BLOCKED"),
    ("https://user:password@docs.example.com/doc", "BLOCKED"),
    ("https://docs.example.com:8443/doc", "BLOCKED"),
    ("https://localhost/doc", "BLOCKED"),
    ("https://foo.localhost/doc", "BLOCKED"),
    ("https://127.0.0.1/doc", "BLOCKED"),
    ("https://169.254.169.254/doc", "BLOCKED"),
    ("https://[::1]/doc", "BLOCKED"),
    ("https://[::ffff:127.0.0.1]/doc", "BLOCKED"),
    ("https://10.0.0.1/doc", "BLOCKED"),
    ("https://docs.example.com.attacker.example/doc", "BLOCKED"),
    ("https://docs.example.com\\@attacker.example/doc", "BLOCKED"),
    ("https://docs.example.com/\nsecret", "BLOCKED"),
])
def test_unsafe_urls_never_make_requests(tmp_path: Path, url: str, status: str) -> None:
    assert store(tmp_path).fetch(spec(url=url), CUTOFF).status == status


def test_live_metadata_is_response_metadata_not_unverified_manifest(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["accept-encoding"] == "identity"
        assert "authorization" not in request.headers
        assert request.extensions["timeout"]["read"] <= 3
        return httpx.Response(200, content=b"exact\r\ntext", headers={
            "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT", "ETag": '"v2"', "Content-Type": "text/plain",
        })
    result = store(tmp_path, handler, request_timeout_seconds=3).fetch(
        spec(last_updated="2020-01-01T00:00:00Z", historical_version_verified=True), CUTOFF,
    )
    assert result.status == "AVAILABLE"
    assert result.content == "exact\r\ntext"
    assert result.last_updated == UPDATED
    assert result.version == '"v2"'
    assert not result.historical_version_verified
    assert json.loads(result.metadata_provenance)["manifest"]["last_updated"].startswith("2020")


@pytest.mark.parametrize("headers,document,historical,status", [
    ({"ETag": '"v1"'}, {"version": "v1", "historical_version_verified": True, "last_updated": UPDATED}, True, "AVAILABLE"),
    ({"ETag": '"v1"'}, {"version": "v1"}, False, "AVAILABLE"),
    ({"X-Document-Version": "revision-3"}, {"version": "revision-3", "historical_version_verified": True,
                                         "last_updated": UPDATED}, True, "AVAILABLE"),
    ({"ETag": 'W/"v1"'}, {"version": "v1", "historical_version_verified": True}, False, "METADATA_MISMATCH"),
    ({"ETag": '"v2"'}, {"version": "v1"}, False, "METADATA_MISMATCH"),
    ({}, {"historical_version_verified": True}, False, "AVAILABLE"),
    ({}, {"snapshot_sha256": hashlib.sha256(b"hello").hexdigest(), "historical_version_verified": True,
          "version": "manifest-revision", "last_updated": UPDATED}, True, "AVAILABLE"),
])
def test_historical_identity_requires_declared_and_verified_binding(
    tmp_path: Path, headers: dict[str, str], document: dict[str, object], historical: bool, status: str,
) -> None:
    result = store(tmp_path, lambda _: httpx.Response(200, content=b"hello", headers=headers)).fetch(spec(**document), CUTOFF)
    assert result.status == status
    assert result.historical_version_verified == historical
    if historical:
        assert result.last_updated == UPDATED


def test_fetched_after_cutoff_does_not_become_historical_from_old_last_modified(tmp_path: Path) -> None:
    result = store(tmp_path, lambda _: httpx.Response(200, text="present-day text", headers={
        "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT",
    })).fetch(spec(), CUTOFF)
    assert result.status == "AVAILABLE"
    assert not result.historical_version_verified
    assert "cutoff" in result.reason


def test_verified_timestamp_conflict_fails(tmp_path: Path) -> None:
    result = store(tmp_path, lambda _: httpx.Response(200, content=b"hello", headers={
        "Last-Modified": "Tue, 02 Jan 2024 00:00:00 GMT",
    })).fetch(spec(snapshot_sha256=hashlib.sha256(b"hello").hexdigest(), last_updated=UPDATED), CUTOFF)
    assert result.status == "METADATA_MISMATCH"


def test_live_declared_hash_mismatch_is_not_cached(tmp_path: Path) -> None:
    result = store(tmp_path, lambda _: httpx.Response(200, text="current content")).fetch(
        spec(snapshot_sha256="0" * 64, historical_version_verified=True), CUTOFF,
    )
    assert result.status == "INTEGRITY_ERROR"
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("headers,status", [
    ({"Last-Modified": "yesterday"}, "MALFORMED_TIMESTAMP"),
    ({"Last-Modified": "Mon, 01 Jan 2024 00:00:00"}, "MALFORMED_TIMESTAMP"),
    ({"Content-Length": "100"}, "TOO_LARGE"),
    ({"Content-Length": "-2"}, "MALFORMED_RESPONSE"),
    ({"Content-Length": "9" * 5000}, "TOO_LARGE"),
    ({"Content-Length": "1"}, "MALFORMED_RESPONSE"),
    ({"Content-Type": "application/pdf"}, "UNSUPPORTED"),
    ({"Content-Encoding": "br"}, "UNSUPPORTED"),
    ({"ETag": '"v1'}, "MALFORMED_RESPONSE"),
    ({"ETag": "v1"}, "MALFORMED_RESPONSE"),
    ({"X-Document-Version": ""}, "MALFORMED_RESPONSE"),
])
def test_live_metadata_errors(tmp_path: Path, headers: dict[str, str], status: str) -> None:
    result = store(tmp_path, lambda _: httpx.Response(200, content=b"hello", headers=headers),
                   max_document_bytes=8).fetch(spec(), CUTOFF)
    assert result.status == status
    assert result.content == ""


class Chunks(httpx.SyncByteStream):
    def __iter__(self):
        yield b"1234"
        yield b"5678"
        yield b"9"


def test_streaming_size_is_bounded_without_content_length(tmp_path: Path) -> None:
    result = store(tmp_path, lambda _: httpx.Response(200, stream=Chunks()),
                   max_document_bytes=8).fetch(spec(), CUTOFF)
    assert result.status == "TOO_LARGE"


def test_exact_size_boundary_is_accepted(tmp_path: Path) -> None:
    assert store(tmp_path, lambda _: httpx.Response(200, content=b"12345678"),
                 max_document_bytes=8).fetch(spec(), CUTOFF).status == "AVAILABLE"


@pytest.mark.parametrize("code,status", [
    (401, "AUTH_REQUIRED"), (403, "AUTH_REQUIRED"), (404, "NOT_FOUND"),
    (410, "NOT_FOUND"), (413, "TOO_LARGE"), (500, "HTTP_ERROR"),
    (206, "MALFORMED_RESPONSE"), (302, "MALFORMED_RESPONSE"),
])
def test_http_errors_are_explicit(tmp_path: Path, code: int, status: str) -> None:
    assert store(tmp_path, lambda _: httpx.Response(code, text="failure")).fetch(spec(), CUTOFF).status == status


@pytest.mark.parametrize("exception,status", [
    (httpx.ReadTimeout, "TIMEOUT"), (httpx.ConnectError, "FETCH_ERROR"),
])
def test_transport_errors_do_not_leak_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exception: type[httpx.RequestError], status: str,
) -> None:
    monkeypatch.setenv("DOC_TOKEN", "private-bearer-value")
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception("private-bearer-value", request=request)
    result = store(tmp_path, handler, document_auth_env="DOC_TOKEN").fetch(spec(), CUTOFF)
    assert result.status == status
    assert "private-bearer-value" not in result.model_dump_json()


def test_configured_missing_auth_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOC_TOKEN", raising=False)
    assert store(tmp_path, document_auth_env="DOC_TOKEN").fetch(spec(), CUTOFF).status == "AUTH_REQUIRED"


def test_auth_and_cookies_never_follow_cross_host_redirects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_TOKEN", "private-bearer-value")
    requests: list[httpx.Request] = []
    targets = ["/next", "https://other.example.com/doc", URL]
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) <= len(targets):
            return httpx.Response(302, headers={"Location": targets[len(requests) - 1],
                                               "Set-Cookie": "session=private-cookie; Domain=example.com"})
        return httpx.Response(200, text="runbook")
    result = store(tmp_path, handler, document_auth_env="DOC_TOKEN",
                   allowed_document_hosts=["docs.example.com", "other.example.com"]).fetch(spec(), CUTOFF)
    assert result.status == "AVAILABLE"
    assert [r.headers.get("authorization") for r in requests] == [
        "Bearer private-bearer-value", "Bearer private-bearer-value", None, None,
    ]
    assert all("cookie" not in r.headers for r in requests)
    assert "private-bearer-value" not in result.model_dump_json()
    assert "private-bearer-value" not in next((tmp_path / "cache" / "documents").glob("*.json")).read_text()


@pytest.mark.parametrize("target", [
    "https://outside.example.com/doc", "http://docs.example.com/doc",
    "https://127.0.0.1/doc", "https://user:pass@docs.example.com/doc",
])
def test_redirects_are_validated_before_contact(tmp_path: Path, target: str) -> None:
    count = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(302, headers={"Location": target})
    result = store(tmp_path, handler).fetch(spec(), CUTOFF)
    assert result.status != "AVAILABLE"
    assert count == 1


def test_redirect_loops_are_bounded(tmp_path: Path) -> None:
    count = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(302, headers={"Location": URL})
    assert store(tmp_path, handler).fetch(spec(), CUTOFF).status == "BLOCKED"
    assert count == 6


def test_replay_never_uses_network_and_preserves_fetch_time(tmp_path: Path) -> None:
    document = spec(version="v1", historical_version_verified=True, last_updated=UPDATED)
    original = store(tmp_path, lambda _: httpx.Response(200, text="runbook", headers={"ETag": '"v1"'})).fetch(document, CUTOFF)
    replayed = store(tmp_path, mode="replay", allowed_document_hosts=[]).fetch(
        document.model_copy(update={"id": "new-id", "step_id": "new-step"}), "2027-01-01T00:00:00Z",
    )
    assert original.status == replayed.status == "AVAILABLE"
    assert original.content == replayed.content
    assert original.content_sha256 == replayed.content_sha256
    assert original.retrieved_at == replayed.retrieved_at
    assert replayed.id == "new-id"
    assert replayed.step_id == "new-step"
    assert replayed.historical_version_verified
    provenance = json.loads(replayed.metadata_provenance)
    assert provenance["source"] == "http"
    assert provenance["manifest"]["id"] == "new-id"
    assert provenance["manifest"]["step_id"] == "new-step"


@pytest.mark.parametrize("verified", [False, True])
def test_live_and_replayed_document_dumps_are_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verified: bool,
) -> None:
    document = spec(version="v1", historical_version_verified=verified, last_updated=UPDATED)
    monkeypatch.setattr("scoring_service.documents.utc_now", lambda: "2025-03-01T00:00:00Z")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/runbook":
            return httpx.Response(302, headers={"Location": "/versioned/v1"})
        return httpx.Response(200, content=b"\xef\xbb\xbfRunbook\r\n", headers={"ETag": '"v1"'})

    original = store(tmp_path, handler).fetch(document, CUTOFF)
    assert original.status == "AVAILABLE"
    cache_path = next((tmp_path / "cache" / "documents").glob("*.json"))
    cached_bytes = cache_path.read_bytes()
    monkeypatch.setattr("scoring_service.documents.utc_now", lambda: "2026-09-17T00:00:00Z")
    for _ in range(2):
        replayed = store(tmp_path, mode="replay", allowed_document_hosts=[]).fetch(document, CUTOFF)
        assert replayed.model_dump() == original.model_dump()
        assert digest({"documents": [replayed.model_dump()]}) == digest({"documents": [original.model_dump()]})
        assert cache_path.read_bytes() == cached_bytes


@pytest.mark.parametrize("verified", [False, True])
def test_local_snapshot_dumps_are_stable_across_live_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verified: bool,
) -> None:
    data = b"\xef\xbb\xbfRunbook\r\n"
    (tmp_path / "doc").write_bytes(data)
    document = spec(snapshot_path="doc",
                    snapshot_sha256=hashlib.sha256(data).hexdigest() if verified else None,
                    version="v1", last_updated=UPDATED, historical_version_verified=verified)
    monkeypatch.setattr("scoring_service.documents.utc_now", lambda: "2025-03-01T00:00:00Z")
    original = store(tmp_path).fetch(document, CUTOFF)
    assert original.status == "AVAILABLE"
    cache_path = next((tmp_path / "cache" / "documents").glob("*.json"))
    cached_bytes = cache_path.read_bytes()
    monkeypatch.setattr("scoring_service.documents.utc_now", lambda: "2026-09-17T00:00:00Z")
    for mode in ("replay", "live", "replay"):
        replayed = store(tmp_path, mode=mode).fetch(document, CUTOFF)
        assert replayed.model_dump() == original.model_dump()
        assert cache_path.read_bytes() == cached_bytes


@pytest.mark.parametrize("mode", ["live", "replay"])
@pytest.mark.parametrize("verified", [False, True])
def test_local_cache_reuse_rejects_changed_snapshot(
    tmp_path: Path, mode: str, verified: bool,
) -> None:
    data = b"original runbook"
    path = tmp_path / "doc"
    path.write_bytes(data)
    document = spec(snapshot_path="doc", snapshot_sha256=hashlib.sha256(data).hexdigest() if verified else None)
    assert store(tmp_path).fetch(document, CUTOFF).status == "AVAILABLE"
    cache_path = next((tmp_path / "cache" / "documents").glob("*.json"))
    cached_bytes = cache_path.read_bytes()
    path.write_bytes(b"changed runbook")
    result = store(tmp_path, mode=mode).fetch(document, CUTOFF)
    assert result.status == "INTEGRITY_ERROR"
    assert result.content == ""
    assert cache_path.read_bytes() == cached_bytes


def test_corrupt_local_cache_is_not_silently_rebuilt(tmp_path: Path) -> None:
    (tmp_path / "doc").write_bytes(b"runbook")
    document = spec(snapshot_path="doc")
    assert store(tmp_path).fetch(document, CUTOFF).status == "AVAILABLE"
    cache_path = next((tmp_path / "cache" / "documents").glob("*.json"))
    cache_path.write_text("{", encoding="utf-8")
    result = store(tmp_path, mode="replay").fetch(document, CUTOFF)
    assert result.status == "CACHE_ERROR"
    assert cache_path.read_text(encoding="utf-8") == "{"


def test_cache_identity_includes_declared_version_and_metadata(tmp_path: Path) -> None:
    original = store(tmp_path, lambda _: httpx.Response(200, text="runbook")).fetch(spec(), CUTOFF)
    assert original.status == "AVAILABLE"
    replay = store(tmp_path, mode="replay")
    for changed in [spec(version="v2"), spec(last_updated=UPDATED),
                    spec(historical_version_verified=True), spec(url=URL + "?v=2")]:
        assert replay.fetch(changed, CUTOFF).status == "CACHE_MISS"


def test_missing_cache_is_explicit(tmp_path: Path) -> None:
    assert store(tmp_path, mode="replay").fetch(spec(), CUTOFF).status == "CACHE_MISS"


@pytest.mark.parametrize("corruption", ["json", "digest", "content", "timestamp", "identity", "status"])
def test_corrupt_cache_never_succeeds(tmp_path: Path, corruption: str) -> None:
    assert store(tmp_path, lambda _: httpx.Response(200, text="runbook")).fetch(spec(), CUTOFF).status == "AVAILABLE"
    path = next((tmp_path / "cache" / "documents").glob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    if corruption == "json":
        path.write_text("{", encoding="utf-8")
    else:
        if corruption == "digest":
            record["sha256"] = "0" * 64
        else:
            payload = record["payload"]
            if corruption == "content":
                payload["evidence"]["content"] = "tampered"
            elif corruption == "timestamp":
                payload["evidence"]["retrieved_at"] = "not-a-date"
            elif corruption == "identity":
                payload["identity"]["version"] = "wrong"
            else:
                payload["evidence"]["status"] = "NOT_FOUND"
            record["sha256"] = digest(payload)
        path.write_text(json.dumps(record), encoding="utf-8")
    result = store(tmp_path, mode="replay").fetch(spec(), CUTOFF)
    assert result.status == "CACHE_ERROR"
    assert result.content == ""


def test_cache_write_error_is_not_success_shaped(tmp_path: Path) -> None:
    (tmp_path / "cache").write_text("not a directory")
    result = store(tmp_path, lambda _: httpx.Response(200, text="runbook")).fetch(spec(), CUTOFF)
    assert result.status == "CACHE_ERROR"
    assert result.content == ""


def test_cache_revalidates_current_size_limit(tmp_path: Path) -> None:
    assert store(tmp_path, lambda _: httpx.Response(200, text="runbook")).fetch(spec(), CUTOFF).status == "AVAILABLE"
    assert store(tmp_path, mode="replay", max_document_bytes=3).fetch(spec(), CUTOFF).status == "CACHE_ERROR"


def test_default_transport_rejects_private_dns_without_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scoring_service.documents.socket.getaddrinfo",
                        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))])
    def no_request(*args: object, **kwargs: object) -> None:
        pytest.fail("Private DNS result reached HTTP client")
    monkeypatch.setattr(httpx.Client, "stream", no_request)
    result = DocumentStore(EvaluationConfig(allowed_document_hosts=["docs.example.com"]),
                           tmp_path, tmp_path / "cache").fetch(spec(), CUTOFF)
    assert result.status == "BLOCKED"


def test_default_transport_pins_public_address_and_preserves_tls_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scoring_service.documents.socket.getaddrinfo",
                        lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])

    @contextmanager
    def response(client: httpx.Client, method: str, url: httpx.URL, **kwargs: object):
        assert method == "GET"
        assert url.host == "8.8.8.8"
        assert kwargs["headers"]["Host"] == "docs.example.com"
        assert kwargs["extensions"] == {"sni_hostname": "docs.example.com"}
        yield httpx.Response(200, text="pinned response", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, "stream", response)
    result = DocumentStore(EvaluationConfig(allowed_document_hosts=["docs.example.com"]),
                           tmp_path, tmp_path / "cache").fetch(spec(), CUTOFF)
    assert result.status == "AVAILABLE"
    assert json.loads(result.metadata_provenance)["final_url"] == URL


def test_dns_resolution_is_bounded_by_request_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = Event()

    def slow_resolution(*args: object, **kwargs: object) -> list:
        release.wait(5)
        return [(2, 1, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr("scoring_service.documents.socket.getaddrinfo", slow_resolution)
    def no_request(*args: object, **kwargs: object) -> None:
        pytest.fail("Timed-out DNS resolution reached HTTP client")
    monkeypatch.setattr(httpx.Client, "stream", no_request)
    start = time.monotonic()
    try:
        result = DocumentStore(EvaluationConfig(allowed_document_hosts=["docs.example.com"], request_timeout_seconds=0.05),
                               tmp_path, tmp_path / "cache").fetch(spec(), CUTOFF)
        assert result.status == "TIMEOUT"
        assert time.monotonic() - start < 1
    finally:
        release.set()


def test_invalid_mode_is_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mode"):
        store(tmp_path, mode="online")
