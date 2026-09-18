"""Escaped, self-contained reports and a loopback-only report-file server."""

from __future__ import annotations

import io
import ipaddress
import math
import os
import socket
import stat
import tempfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .models import BatchResult, CaseResult, StepResult

_ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "report.css": "text/css; charset=utf-8",
    "report.js": "text/javascript; charset=utf-8",
}
_CSP = "default-src 'none'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
_DIMENSIONS = {
    "faithfulness": ("Faithfulness", "How faithfully the response's claims reflect the evidence actually obtained."),
    "coverage": ("Coverage", "How completely the response's material claims are backed by recorded claim support."),
    "source_trust": ("Source trust", "How much trust the cited sources earn under the recorded source-trust policy."),
    "freshness": ("Freshness", "How current the referenced documents were at the response cutoff, including historical-version checks."),
}
_STRUCTURAL = {"MISSING_REQUIRED", "UNASSIGNED"}


def _number(value: float | None) -> str:
    return "Unavailable" if value is None or not math.isfinite(value) else f"{value:.2f}"


def _brief(value: object, limit: int = 180) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _dimension_reasons(case: CaseResult, key: str, steps: list[StepResult]) -> list[str]:
    """Keep at most two distinct brief explanations, without per-model repetition."""
    reasons: list[str] = []

    def add(value: object) -> None:
        text = _brief(value)
        if text and text not in reasons and len(reasons) < 2:
            reasons.append(text)

    ids = {step.id for step in steps}
    if key in {"faithfulness", "source_trust"}:
        for judge in case.judges:
            if judge.stage == "step" and judge.step_id in ids:
                if key == "faithfulness":
                    add(judge.output.get("rationale"))
                elif judge.role == "gpt":
                    add(judge.output.get("trust_rationale"))
    elif key == "coverage":
        for step in steps:
            for support in step.support:
                add(support.rationale)
    else:
        for step in steps:
            add(step.freshness_reason.removeprefix("NO_DOCUMENT_NEUTRAL_CONVENTION:"))
        if not reasons:
            for document in case.documents:
                if document.step_id in ids:
                    add(document.reason)
    return reasons


def _dimension_view(case: CaseResult, key: str, weight: int) -> dict[str, Any]:
    """Build a dimension candidate; _case_view verifies the complete breakdown."""
    included = [step for step in case.steps if step.included]
    structural = [step for step in included if step.disposition in _STRUCTURAL and step.score == 0]
    evaluated = [
        step for step in included
        if step.disposition not in _STRUCTURAL
        and step.score is not None and math.isfinite(step.score)
        and getattr(step, key) is not None
    ]
    missing = len(included) - len(structural) - len(evaluated)
    value_sum = math.fsum(getattr(step, key) for step in evaluated)
    name, definition = _DIMENSIONS[key]
    points = None
    label = "Unavailable"
    if case.status == "GATE_FAILED":
        label = "Not computed"
        calculation = "Todo gate failed. The gate sets the incident score; no dimension contributions were computed."
    elif case.status != "SCORED":
        label = "Not computed"
        calculation = f"Final dimension contributions were not computed for {case.status}. Missing scores are not zero."
    elif case.score is None or not math.isfinite(case.score):
        calculation = "No final scored incident result is available. Missing scores are not zero."
    elif not included:
        calculation = "No included steps; a contribution cannot be calculated."
    elif missing:
        calculation = f"Cannot calculate: {missing} of {len(included)} included steps lack a scored {name.lower()} value. Missing scores are not zero."
    else:
        points = weight * value_sum / len(included)
        label = _number(points)
        calculation = f"{weight} x {value_sum:g} / {len(included)} included steps = {_number(points)} contribution points."
    notes = []
    if structural:
        notes.append(f"{len(structural)} required/unassigned steps add no points but remain included.")
    document_steps = {document.step_id for document in case.documents}
    neutral = sum(step.freshness == 1 and step.id not in document_steps for step in evaluated) if key == "freshness" else 0
    if neutral:
        notes.append(f"{neutral} evaluated steps use freshness 1 as a neutral no-document convention, not verified freshness.")
    if key == "faithfulness" and evaluated:
        if any(len(set(step.votes.values())) > 1 for step in evaluated):
            notes.append("Model judgments differed; each step uses the median vote.")
        else:
            notes.append("Each step uses the median model faithfulness vote.")
    summary = []
    if evaluated:
        summary.append(f"{len(evaluated)} contributing step{'s' if len(evaluated) != 1 else ''}")
    if missing:
        summary.append(f"{missing} step{'s' if missing != 1 else ''} with unavailable scores")
    excluded = len(case.steps) - len(included)
    if excluded:
        summary.append(f"{excluded} excluded step{'s' if excluded != 1 else ''}")
    return {
        "key": key,
        "name": name,
        "definition": definition,
        "weight": weight,
        "points": points,
        "label": label,
        "calculation": calculation,
        "included_count": len(included),
        "structural_count": len(structural),
        "missing_count": missing,
        "neutral_count": neutral,
        "distribution": "; ".join(summary) + "." if summary else "",
        "notes": notes if case.status == "SCORED" else [],
        "reasons": _dimension_reasons(case, key, evaluated) if case.status == "SCORED" else [],
    }


def _breakdown_consistency(case: CaseResult, weights: dict[str, int]) -> bool | None:
    """True for a verified breakdown, False for a mismatch, None for missing data."""
    included = [step for step in case.steps if step.included]
    if not included or case.score is None or not math.isfinite(case.score):
        return None
    scores: list[float] = []
    incomplete = False
    for step in included:
        if step.score is None or not math.isfinite(step.score):
            incomplete = True
            continue
        scores.append(step.score)
        if step.disposition in _STRUCTURAL:
            if step.score != 0:
                return False
            continue
        values = [getattr(step, key) for key in _DIMENSIONS]
        if any(value is None for value in values):
            incomplete = True
            continue
        expected = math.fsum(weights[key] * value for key, value in zip(_DIMENSIONS, values))
        if not math.isclose(expected, step.score, rel_tol=0, abs_tol=1e-9):
            return False
    if len(scores) == len(included) and not math.isclose(
        math.fsum(scores) / len(included), case.score, rel_tol=0, abs_tol=1e-9,
    ):
        return False
    return None if incomplete else True


def _case_view(case: CaseResult, weights: dict[str, int]) -> dict[str, Any]:
    """Return a verified compact scorecard; withhold unverifiable contributions."""
    dimensions = [_dimension_view(case, key, weights[key]) for key in _DIMENSIONS]
    score = case.score if case.status in {"SCORED", "GATE_FAILED"} else None
    if score is not None and not math.isfinite(score):
        score = None
    consistency_state = "unavailable"
    consistency = "Contribution sum unavailable; missing scores have not been replaced with zero."
    status_note = {
        "SCORED": "",
        "GATE_FAILED": "Todo gate failed. Dimension contributions were not computed.",
        "UNSCORABLE": "Insufficient evidence for a final score.",
        "JUDGE_ERROR": "Model evaluation failed; the incident score is unavailable.",
        "IMPORT_ERROR": "Input import failed; the incident score is unavailable.",
        "NOT_APPLICABLE": "No applicable incident score.",
    }[case.status]
    if case.status == "GATE_FAILED":
        consistency_state = "not-evaluated"
        consistency = "Gate outcome only; no dimension sum."
        reasons = [_brief(vote.rationale) for vote in case.gate_votes.values() if vote.decision == "FAIL"][:2]
        if reasons:
            status_note += " " + " ".join(reasons)
    elif score is not None:
        verified = _breakdown_consistency(case, weights)
        if verified is True:
            total = math.fsum(dimension["points"] for dimension in dimensions)
            consistency_state = "match"
            consistency = f"Contribution sum: {_number(total)} pt; recorded incident score: {_number(score)}. Matches before display rounding."
        else:
            consistency_state = "mismatch" if verified is False else "unavailable"
            issue = "inconsistent" if verified is False else "incomplete"
            status_note = f"Review required: the recorded step breakdown is {issue}. Contribution points are unavailable."
            consistency = status_note + " The recorded incident score has not been changed."
            for dimension in dimensions:
                dimension["points"] = None
                dimension["label"] = "Unavailable"
                dimension["calculation"] = status_note + " Missing scores are not zero; no contribution points are inferred."
    return {
        "case_id": _brief(case.case_id, 80),
        "incident_id": _brief(case.incident_id, 96),
        "message_id": _brief(case.message_id, 96),
        "synthetic": case.synthetic,
        "status": str(case.status),
        "score": score,
        "status_note": status_note,
        "dimensions": dimensions,
        "consistency_state": consistency_state,
        "consistency": consistency,
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
    """Write compact index.html, report.css and report.js; leave CLI-owned files alone."""
    package = Path(__file__).parent
    environment = Environment(
        loader=FileSystemLoader(package / "templates"),
        autoescape=select_autoescape(("html", "xml", "j2"), default=True),
        undefined=StrictUndefined,
    )
    environment.filters.update(number=_number)
    real_count = sum(not case.synthetic for case in batch.results)
    # Never pass BatchResult/model dumps to the template: even inert payloads leak raw data.
    html = environment.get_template("report.html.j2").render(
        cases=[_case_view(case, batch.weights) for case in batch.results],
        real_count=real_count,
        has_synthetic=any(case.synthetic for case in batch.results),
        target_real_cases=batch.target_real_cases,
        runtime_log_name=_brief(PureWindowsPath(batch.runtime_log_file).name, 80) if batch.runtime_log_file else None,
    )
    assets = {name: (package / "assets" / name).read_bytes() for name in _ASSETS if name != "index.html"}
    output_dir = output_dir.absolute()
    _reject_links(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Validate every destination before replacing any existing report asset.
    for name in _ASSETS:
        destination = output_dir / name
        _reject_links(destination)
        if destination.exists():
            _plain_file(destination)
    for name, content in assets.items():
        _write_asset(output_dir, name, content)
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
            raise ValueError(f"Report directory must contain index.html, report.css and report.js: {directory}") from error
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
            if path not in {"/", *(f"/{asset}" for asset in _ASSETS)} or name not in _ASSETS:
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
    """Block serving only a rendered directory's three UI assets on a loopback address.

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
