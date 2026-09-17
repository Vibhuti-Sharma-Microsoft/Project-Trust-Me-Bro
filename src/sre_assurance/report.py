"""Escaped, self-contained reports and a loopback-only report-file server."""

from __future__ import annotations

import io
import ipaddress
import json
import math
import os
import socket
import stat
import tempfile
from collections import Counter
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .models import BatchResult, CaseResult

_ASSETS = {"index.html": "text/html; charset=utf-8", "report.css": "text/css; charset=utf-8"}
_STATUSES = ("SCORED", "GATE_FAILED", "UNSCORABLE", "JUDGE_ERROR", "IMPORT_ERROR", "NOT_APPLICABLE")
_CSP = "default-src 'none'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"


def _number(value: float | None) -> str:
    return "Unavailable" if value is None or not math.isfinite(value) else f"{value:.2f}"


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)


def _source_url(value: str) -> str | None:
    if not value or "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        parsed.port  # Reject malformed ports as well as malformed host syntax.
    except ValueError:
        return None
    return value


def _case_view(case: CaseResult) -> dict[str, Any]:
    included_count = sum(step.included for step in case.steps)
    steps = []
    for step in case.steps:
        score = step.score
        contribution = (
            score / included_count
            if step.included and included_count and score is not None and math.isfinite(score)
            else None
        )
        steps.append(
            {
                "result": step.model_dump(mode="json"),
                "contribution": contribution,
                "disagreement": len(set(step.votes.values())) > 1,
                "has_documents": any(document.step_id == step.id for document in case.documents),
            }
        )
    return {
        "result": case.model_dump(mode="json"),
        "included_count": included_count,
        "steps": steps,
        "gate_disagreement": len({vote.decision for vote in case.gate_votes.values()}) > 1,
    }


def _reject_links(path: Path) -> None:
    # Windows junctions/reparse points must be rejected as well as POSIX symlinks.
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError(f"Report paths must not contain symlinks or reparse points: {component}")


def _plain_file(path: Path) -> os.stat_result:
    _reject_links(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(f"Report assets must be ordinary, non-linked files: {path}")
    return metadata


def _write_asset(directory: Path, name: str, content: bytes) -> None:
    destination = directory / name
    _reject_links(destination)
    if destination.exists():
        _plain_file(destination)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        _reject_links(directory)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def render_report(batch: BatchResult, output_dir: Path) -> Path:
    """Write index.html and report.css; null scores and all case statuses are retained."""
    package = Path(__file__).parent
    environment = Environment(
        loader=FileSystemLoader(package / "templates"),
        autoescape=select_autoescape(("html", "xml", "j2"), default=True),
        undefined=StrictUndefined,
    )
    environment.filters.update(number=_number, json_text=_json_text, source_url=_source_url)
    counts = Counter(case.status for case in batch.results)
    real_count = sum(not case.synthetic for case in batch.results)
    scored = [
        case.score
        for case in batch.results
        if case.status == "SCORED" and case.score is not None and math.isfinite(case.score)
    ]
    html = environment.get_template("report.html.j2").render(
        batch=batch.model_dump(mode="json"),
        cases=[_case_view(case) for case in batch.results],
        statuses=_STATUSES,
        counts=counts,
        real_count=real_count,
        synthetic_count=len(batch.results) - real_count,
        scored_count=len(scored),
        mean_score=sum(scored) / len(scored) if scored else None,
    )
    css = (package / "assets" / "report.css").read_bytes()
    output_dir = output_dir.absolute()
    _reject_links(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Validate both destinations before replacing either existing report asset.
    for name in _ASSETS:
        destination = output_dir / name
        _reject_links(destination)
        if destination.exists():
            _plain_file(destination)
    _write_asset(output_dir, "report.css", css)
    _write_asset(output_dir, "index.html", html.encode("utf-8"))
    return output_dir / "index.html"


def _report_directory(directory: Path) -> Path:
    directory = directory.absolute()
    _reject_links(directory)
    if not directory.is_dir():
        raise ValueError(f"Report directory does not exist or is not a directory: {directory}")
    directory = directory.resolve(strict=True)
    for name in _ASSETS:
        try:
            _plain_file(directory / name)
        except FileNotFoundError as error:
            raise ValueError(f"Report directory must contain index.html and report.css: {directory}") from error
    return directory


def _loopback_host(host: str) -> str:
    if host.lower() == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("Report host must be a loopback IP address or localhost") from error
    if not address.is_loopback or "%" in host:
        raise ValueError("Report host must be a loopback IP address or localhost")
    return str(address)


class _ReportHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, directory: str, **kwargs: Any) -> None:
        self._root = Path(directory)
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def list_directory(self, path: str | os.PathLike[str]) -> io.BytesIO | None:
        self.send_error(403, "Directory listings are disabled")
        return None

    def send_head(self) -> BinaryIO | None:
        try:
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                raise ValueError("A single loopback Host header is required")
            host = urlsplit("//" + hosts[0])
            if host.username is not None or host.password is not None or host.path or host.query or host.fragment:
                raise ValueError("Invalid Host header")
            _loopback_host(host.hostname or "")
            address = self.server.server_address
            if host.port is not None and (not isinstance(address, tuple) or host.port != address[1]):
                raise ValueError("Invalid Host port")
        except ValueError:
            self.send_error(403, "Only loopback report requests are allowed")
            return None
        try:
            request = urlsplit(self.path)
            path = unquote(request.path, errors="strict")
            if request.scheme or request.netloc:
                raise ValueError("Absolute request URLs are not allowed")
            name = "index.html" if path == "/" else path.removeprefix("/")
            if path not in {"/", "/index.html", "/report.css"} or name not in _ASSETS:
                raise ValueError("Only report assets are served")
            # Revalidate on every request, including after a file/directory was replaced.
            root = _report_directory(self._root)
            asset = root / name
            before = _plain_file(asset)
            with asset.open("rb") as stream:
                if not os.path.samestat(before, os.fstat(stream.fileno())):
                    raise ValueError("Report asset changed while opening")
                _reject_links(asset)
                if not os.path.samestat(before, asset.lstat()):
                    raise ValueError("Report asset changed while opening")
                content = stream.read()
        except (OSError, ValueError):
            self.send_error(404, "Report asset not found or not a regular local file")
            return None
        self.send_response(200)
        self.send_header("Content-Type", _ASSETS[name])
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        return io.BytesIO(content)


class _IPv6ReportServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def serve_reports(directory: Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Block serving only a rendered directory's two assets on a loopback address.

    Arbitrary files, subdirectories and directory listings are never exposed.
    Invalid hosts, ports, directories and linked assets raise before binding.
    KeyboardInterrupt stops the server and closes its listening socket.
    """
    host = _loopback_host(host)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Report port must be an integer between 0 and 65535")
    directory = _report_directory(directory)
    server_type = _IPv6ReportServer if ":" in host else ThreadingHTTPServer
    handler = partial(_ReportHandler, directory=str(directory))
    with server_type((host, port), handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
