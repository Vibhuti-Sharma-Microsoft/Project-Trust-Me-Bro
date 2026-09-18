"""Read-only, integrity-checked document snapshots; freshness is scored elsewhere."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import EvaluationConfig, digest, stable_json
from .imports import safe_path
from .models import DocumentEvidence, DocumentSpec
from .time_utils import timestamp_ns, utc_now

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_ETAG = re.compile(r'^(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"$')
_CONTENT_TYPES = {
    "text/plain", "text/markdown", "text/x-markdown", "text/html",
    "application/xhtml+xml", "application/json",
}
_REDIRECTS = {301, 302, 303, 307, 308}


class _DocumentError(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class DocumentStore:
    """Fetch only manifest-selected documents, or replay local/cached UTF-8 snapshots.

    The cache is an integrity-checked local artifact, not a signed trust authority.
    An explicit historical declaration must be bound to a matching content hash or
    a strong response version. Last-Modified alone never proves historical identity.
    Injected transports are trusted caller infrastructure. Default live requests
    pin a validated public DNS address, retaining the original Host and TLS SNI.
    Only uncompressed UTF-8 text on HTTPS port 443 is supported.
    Reusing a validated snapshot preserves its original metadata and retrieved_at,
    so cache mechanics do not change judge request hashes. Local files are still
    revalidated; changed bytes under the same cache identity fail explicitly.
    """

    def __init__(
        self,
        config: EvaluationConfig,
        corpus_root: Path,
        cache_dir: Path,
        mode: str = "live",
        transport: httpx.BaseTransport | None = None,
    ):
        if mode not in {"live", "replay"}:
            raise ValueError("Document mode must be 'live' or 'replay'")
        self.config = config
        self.corpus_root = Path(corpus_root)
        self.cache_dir = Path(cache_dir)
        self.mode = mode
        self.transport = transport

    def fetch(self, spec: DocumentSpec, cutoff: str) -> DocumentEvidence:
        """Return AVAILABLE or an explicit failure; never make a replay request."""
        provenance: dict[str, Any] = {"manifest": spec.model_dump(mode="json")}
        try:
            self._timestamp(cutoff)
            if spec.last_updated is not None:
                self._timestamp(spec.last_updated)
            if spec.snapshot_sha256 is not None and not _SHA256.fullmatch(spec.snapshot_sha256):
                raise _DocumentError("INTEGRITY_ERROR", "Manifest snapshot SHA-256 is malformed.")
            self._url(spec.url, require_allowlist=False)
            if spec.snapshot_path is not None:
                return self._local(spec, provenance)
            elif self.mode == "replay":
                provenance["source"] = "cache"
                return self._replay(spec)
            else:
                evidence = self._live(spec, provenance)
            provenance["retrieved_at"] = evidence.retrieved_at
            self._cache(spec, evidence)
            return evidence
        except _DocumentError as exc:
            return DocumentEvidence(
                id=spec.id, url=spec.url, step_id=spec.step_id,
                status=exc.status, reason=exc.reason,
                metadata_provenance=stable_json(provenance),
            )

    @staticmethod
    def _timestamp(value: str) -> None:
        try:
            timestamp_ns(value)
        except (ValueError, OverflowError) as exc:
            raise _DocumentError("MALFORMED_TIMESTAMP", "Document or cutoff timestamp is not valid zoned RFC3339.") from exc

    def _url(self, value: str, *, require_allowlist: bool) -> httpx.URL:
        try:
            if "\\" in value or any(ord(char) < 33 or ord(char) == 127 for char in value):
                raise _DocumentError("BLOCKED", "URL contains unsafe whitespace or separators.")
            parts = urlsplit(value)
            if parts.username is not None or parts.password is not None:
                raise _DocumentError("BLOCKED", "Credentials in document URLs are forbidden.")
            if parts.scheme.lower() != "https":
                status = "UNSUPPORTED" if parts.scheme.lower() == "http" else "BLOCKED"
                raise _DocumentError(status, "Only HTTPS document URLs are supported.")
            url = httpx.URL(value)
            host = url.host.lower().rstrip(".")
            if not host or "%" in host or url.port not in (None, 443):
                raise _DocumentError("BLOCKED", "Document URL must name an HTTPS host on port 443.")
            if host in {"localhost", "localhost.localdomain", "ip6-localhost"} or host.endswith(
                (".localhost", ".local", ".internal")
            ):
                raise _DocumentError("BLOCKED", "Local document hosts are forbidden.")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and not self._public_address(address):
                raise _DocumentError("BLOCKED", "Non-public document addresses are forbidden.")
            allowed = {entry.lower().rstrip(".") for entry in self.config.allowed_document_hosts}
            if require_allowlist and host not in allowed:
                raise _DocumentError("BLOCKED", "Document host is not in the configured allowlist.")
            return url.copy_with(host=host, fragment=None)
        except (ValueError, httpx.InvalidURL) as exc:
            raise _DocumentError("BLOCKED", "Document URL is malformed.") from exc

    @staticmethod
    def _public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return address.is_global and not address.is_multicast

    def _request_target(self, url: httpx.URL, deadline: float) -> httpx.URL:
        if self.transport is not None:
            return url
        resolved: Queue[str | _DocumentError] = Queue(maxsize=1)

        def resolve() -> None:
            try:
                addresses = [
                    ipaddress.ip_address(entry[4][0])
                    for entry in socket.getaddrinfo(url.host, 443, type=socket.SOCK_STREAM)
                ]
                if not addresses or any(not self._public_address(address) for address in addresses):
                    resolved.put(_DocumentError("BLOCKED", "Document host resolves to a non-public address."))
                else:
                    # Pin the checked address so the HTTP connection cannot re-resolve it.
                    resolved.put(str(addresses[0]))
            except (OSError, ValueError):
                resolved.put(_DocumentError("FETCH_ERROR", "Document host could not be resolved."))

        Thread(target=resolve, daemon=True).start()
        try:
            result = resolved.get(timeout=max(0, deadline - time.monotonic()))
        except Empty as exc:
            raise _DocumentError("TIMEOUT", "Document DNS resolution exceeded the configured timeout.") from exc
        if isinstance(result, _DocumentError):
            raise result
        return url.copy_with(host=result)

    def _content(self, data: bytes, spec: DocumentSpec) -> tuple[str, str]:
        if len(data) > self.config.max_document_bytes:
            raise _DocumentError("TOO_LARGE", "Document exceeds max_document_bytes.")
        actual_hash = hashlib.sha256(data).hexdigest()
        if spec.snapshot_sha256 is not None and actual_hash != spec.snapshot_sha256.lower():
            raise _DocumentError("INTEGRITY_ERROR", "Document does not match its declared SHA-256.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _DocumentError("UNSUPPORTED", "Only exact, UTF-8 document text is supported.") from exc
        if "\x00" in text:
            raise _DocumentError("UNSUPPORTED", "Binary document content is unsupported.")
        if not text.strip("\ufeff \t\r\n"):
            raise _DocumentError("EMPTY_CONTENT", "Document contains no usable text.")
        return text, actual_hash

    def _local(self, spec: DocumentSpec, provenance: dict[str, Any]) -> DocumentEvidence:
        provenance["source"] = "local_snapshot"
        assert spec.snapshot_path is not None
        try:
            path = safe_path(self.corpus_root, spec.snapshot_path)
            with path.open("rb") as handle:
                data = handle.read(self.config.max_document_bytes + 1)
        except FileNotFoundError as exc:
            raise _DocumentError("NOT_FOUND", "Declared local document snapshot was not found.") from exc
        except ValueError as exc:
            raise _DocumentError("BLOCKED", "Snapshot path is outside the permitted corpus.") from exc
        except OSError as exc:
            raise _DocumentError("READ_ERROR", "Local document snapshot could not be read.") from exc
        text, content_hash = self._content(data, spec)
        try:
            cached = self._replay(spec)
        except _DocumentError as exc:
            if exc.status != "CACHE_MISS":
                raise
        else:
            if cached.content_sha256 != content_hash:
                raise _DocumentError("INTEGRITY_ERROR", "Local snapshot changed under the same source and declared metadata.")
            return cached
        verified = spec.snapshot_sha256 is not None
        provenance["manifest_binding"] = "sha256" if verified else "unverified"
        evidence = self._available(
            spec, text, content_hash, spec.last_updated if verified else None,
            spec.version if verified else None, verified, provenance,
        )
        provenance["retrieved_at"] = evidence.retrieved_at
        self._cache(spec, evidence)
        return evidence

    def _available(
        self, spec: DocumentSpec, text: str, content_hash: str,
        last_updated: str | None, version: str | None, verified: bool,
        provenance: dict[str, Any],
    ) -> DocumentEvidence:
        historical = spec.historical_version_verified and verified
        limitations = []
        if not historical:
            limitations.append("Historical version is not verified; retrieval alone does not establish content at the cutoff.")
        if last_updated is None:
            limitations.append("No verified last-updated timestamp is available.")
        if not verified and any((spec.last_updated, spec.version, spec.historical_version_verified)):
            limitations.append("Unverified manifest metadata is retained in provenance, not asserted as actual metadata.")
        return DocumentEvidence(
            id=spec.id, url=spec.url, step_id=spec.step_id, status="AVAILABLE",
            content=text, content_sha256=content_hash, last_updated=last_updated,
            retrieved_at=utc_now(), historical_version_verified=historical,
            version=version, metadata_provenance=stable_json(provenance),
            reason=" ".join(limitations),
        )

    def _live(self, spec: DocumentSpec, provenance: dict[str, Any]) -> DocumentEvidence:
        provenance["source"] = "http"
        current = self._url(spec.url, require_allowlist=True)
        headers = {"Accept": "text/plain, text/markdown, text/html, application/json", "Accept-Encoding": "identity"}
        if self.config.document_auth_env:
            token = os.environ.get(self.config.document_auth_env)
            if not token or not token.strip():
                raise _DocumentError("AUTH_REQUIRED", "Configured document bearer credential is unavailable.")
            if any(ord(char) < 33 or ord(char) > 126 for char in token):
                raise _DocumentError("AUTH_REQUIRED", "Configured document bearer credential is malformed.")
            headers["Authorization"] = f"Bearer {token}"
        deadline = time.monotonic() + self.config.request_timeout_seconds
        provenance["redirects"] = []
        try:
            with httpx.Client(
                transport=self.transport, timeout=self.config.request_timeout_seconds,
                follow_redirects=False, trust_env=False,
            ) as client:
                for hop in range(6):
                    request_target = self._request_target(current, deadline)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _DocumentError("TIMEOUT", "Document request exceeded the configured timeout.")
                    client.cookies.clear()
                    request_headers = {**headers, "Host": current.netloc.decode("ascii")}
                    with client.stream(
                        "GET", request_target, headers=request_headers, timeout=remaining,
                        extensions={"sni_hostname": current.raw_host.decode("ascii")},
                    ) as response:
                        if response.status_code in _REDIRECTS:
                            if hop == 5:
                                raise _DocumentError("BLOCKED", "Document redirect limit exceeded.")
                            location = response.headers.get("location")
                            if not location:
                                raise _DocumentError("MALFORMED_RESPONSE", "Document redirect has no Location.")
                            target = self._url(str(current.join(location)), require_allowlist=True)
                            if target.host != current.host:
                                headers.pop("Authorization", None)
                            provenance["redirects"].append(str(target))
                            current = target
                            continue
                        if response.status_code in (401, 403):
                            raise _DocumentError("AUTH_REQUIRED", "Document server requires valid authentication or additional permission.")
                        if response.status_code in (404, 410):
                            raise _DocumentError("NOT_FOUND", "Document server reports the document is unavailable.")
                        if response.status_code == 413:
                            raise _DocumentError("TOO_LARGE", "Document server rejected the document size.")
                        if not 200 <= response.status_code < 300:
                            raise _DocumentError("HTTP_ERROR", f"Document server returned HTTP {response.status_code}.")
                        if response.status_code == 206:
                            raise _DocumentError("MALFORMED_RESPONSE", "Partial document responses are not complete snapshots.")
                        provenance["final_url"] = str(current)
                        return self._response(spec, response, provenance, deadline)
        except httpx.TimeoutException as exc:
            raise _DocumentError("TIMEOUT", "Document request exceeded the configured timeout.") from exc
        except (httpx.RequestError, httpx.InvalidURL) as exc:
            raise _DocumentError("FETCH_ERROR", f"Document request failed ({type(exc).__name__}).") from exc
        raise _DocumentError("BLOCKED", "Document redirect limit exceeded.")

    def _response(
        self, spec: DocumentSpec, response: httpx.Response,
        provenance: dict[str, Any], deadline: float,
    ) -> DocumentEvidence:
        provenance["response_metadata"] = {
            "last-modified": response.headers.get("last-modified"),
            "etag": response.headers.get("etag"),
            "x-document-version": response.headers.get("x-document-version"),
            "content-type": response.headers.get("content-type"),
        }
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type not in _CONTENT_TYPES:
            raise _DocumentError("UNSUPPORTED", "Response media type is not a supported text document.")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise _DocumentError("UNSUPPORTED", "Compressed document responses are not supported; identity encoding is required.")
        length = response.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                raise _DocumentError("MALFORMED_RESPONSE", "Document Content-Length is malformed.")
            length = length.lstrip("0") or "0"
            if len(length) > len(str(self.config.max_document_bytes)) or int(length) > self.config.max_document_bytes:
                raise _DocumentError("TOO_LARGE", "Document exceeds max_document_bytes.")
        data = bytearray()
        for chunk in response.iter_bytes(chunk_size=min(self.config.max_document_bytes + 1, 65536)):
            if time.monotonic() > deadline:
                raise _DocumentError("TIMEOUT", "Document request exceeded the configured timeout.")
            if len(data) + len(chunk) > self.config.max_document_bytes:
                raise _DocumentError("TOO_LARGE", "Document exceeds max_document_bytes.")
            data.extend(chunk)
        if length is not None and len(data) != int(length):
            raise _DocumentError("MALFORMED_RESPONSE", "Document body does not match Content-Length.")
        text, content_hash = self._content(bytes(data), spec)
        last_modified = response.headers.get("last-modified")
        updated = self._last_modified(last_modified) if last_modified is not None else None
        etag = response.headers.get("etag")
        if etag is not None and not _ETAG.fullmatch(etag):
            raise _DocumentError("MALFORMED_RESPONSE", "Response ETag is not a valid entity tag.")
        document_version = response.headers.get("x-document-version")
        if document_version is not None and (
            not document_version or any(ord(char) < 32 or ord(char) == 127 for char in document_version)
        ):
            raise _DocumentError("MALFORMED_RESPONSE", "Response document version is malformed.")
        response_version = document_version or etag
        version_verified = bool(
            spec.version and (
                document_version == spec.version
                or (document_version is None and etag and not etag.startswith("W/")
                    and spec.version in (etag, etag[1:-1]))
            )
        )
        hash_verified = spec.snapshot_sha256 is not None
        verified = hash_verified or version_verified
        if spec.version and response_version and not verified:
            raise _DocumentError("METADATA_MISMATCH", "Response version does not verify the declared document version.")
        if verified and spec.last_updated and updated and timestamp_ns(spec.last_updated) != timestamp_ns(updated):
            raise _DocumentError("METADATA_MISMATCH", "Response last-updated timestamp conflicts with verified manifest metadata.")
        provenance["manifest_binding"] = "sha256" if hash_verified else "response_version" if version_verified else "unverified"
        return self._available(
            spec, text, content_hash, updated or (spec.last_updated if verified else None),
            spec.version if verified and spec.version else response_version,
            verified, provenance,
        )

    def _last_modified(self, value: str) -> str:
        try:
            timestamp_ns(value)
            return value
        except (ValueError, OverflowError):
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    raise ValueError("Missing timezone")
                result = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                self._timestamp(result)
                return result
            except (ValueError, TypeError, OverflowError) as exc:
                raise _DocumentError("MALFORMED_TIMESTAMP", "Response Last-Modified is not a valid zoned timestamp.") from exc

    @staticmethod
    def _identity(spec: DocumentSpec) -> dict[str, Any]:
        identity = spec.model_dump(mode="json", exclude={"id", "step_id"})
        if spec.snapshot_sha256 is not None:
            identity["snapshot_sha256"] = spec.snapshot_sha256.lower()
        return identity

    def _cache_path(self, spec: DocumentSpec) -> Path:
        try:
            root = self.cache_dir.resolve()
            directory = root / "documents"
            path = directory / f"{digest(self._identity(spec))}.json"
            if not directory.resolve().is_relative_to(root) or path.is_symlink():
                raise _DocumentError("CACHE_ERROR", "Document cache path is not a safe local artifact.")
            return path
        except (OSError, ValueError, RuntimeError) as exc:
            raise _DocumentError("CACHE_ERROR", "Document cache path could not be resolved.") from exc

    def _cache(self, spec: DocumentSpec, evidence: DocumentEvidence) -> None:
        path = self._cache_path(spec)
        payload = {"schema_version": 1, "identity": self._identity(spec), "evidence": evidence.model_dump(mode="json")}
        record = stable_json({"payload": payload, "sha256": digest(payload)})
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(record)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise _DocumentError("CACHE_ERROR", "Validated document snapshot could not be persisted in cache.") from exc
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError as exc:
                    raise _DocumentError("CACHE_ERROR", "Temporary document cache artifact could not be removed.") from exc

    def _replay(self, spec: DocumentSpec) -> DocumentEvidence:
        path = self._cache_path(spec)
        try:
            limit = self.config.max_document_bytes * 6 + 65536
            with path.open("rb") as handle:
                raw = handle.read(limit + 1)
            if len(raw) > limit:
                raise _DocumentError("CACHE_ERROR", "Document cache record exceeds the permitted size.")
            record = json.loads(raw)
            if not isinstance(record, dict) or set(record) != {"payload", "sha256"}:
                raise ValueError("Invalid cache envelope")
            payload = record["payload"]
            if not isinstance(payload, dict) or set(payload) != {"schema_version", "identity", "evidence"}:
                raise ValueError("Invalid cache payload")
            if digest(payload) != record["sha256"] or payload["schema_version"] != 1:
                raise ValueError("Cache integrity mismatch")
            if payload["identity"] != self._identity(spec):
                raise ValueError("Cache identity mismatch")
            evidence = DocumentEvidence.model_validate(payload["evidence"])
            if evidence.status != "AVAILABLE" or evidence.url != spec.url or evidence.retrieved_at is None:
                raise ValueError("Invalid cached snapshot")
            timestamp_ns(evidence.retrieved_at)
            if evidence.last_updated is not None:
                timestamp_ns(evidence.last_updated)
            _, actual_hash = self._content(evidence.content.encode("utf-8"), spec)
            if evidence.content_sha256 != actual_hash:
                raise ValueError("Cache content integrity mismatch")
            cached_provenance = json.loads(evidence.metadata_provenance)
            if not isinstance(cached_provenance, dict):
                raise ValueError("Invalid cached provenance")
            if evidence.historical_version_verified and (
                not spec.historical_version_verified
                or cached_provenance.get("manifest_binding") not in {"sha256", "response_version"}
            ):
                raise ValueError("Invalid cached historical binding")
        except FileNotFoundError as exc:
            raise _DocumentError("CACHE_MISS", "No cached document snapshot matches this source and declared metadata; replay never uses the network.") from exc
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise _DocumentError("CACHE_ERROR", "Document cache record is unreadable, malformed, or fails integrity validation.") from exc
        except _DocumentError as exc:
            raise _DocumentError("CACHE_ERROR", f"Cached document validation failed ({exc.status}).") from exc
        evidence.id = spec.id
        evidence.step_id = spec.step_id
        manifest = spec.model_dump(mode="json")
        if cached_provenance.get("manifest") != manifest:
            cached_provenance["manifest"] = manifest
            evidence.metadata_provenance = stable_json(cached_provenance)
        return evidence
