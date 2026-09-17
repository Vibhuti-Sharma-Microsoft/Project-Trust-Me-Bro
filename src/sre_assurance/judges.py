from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .config import EvaluationConfig, ModelEndpoint, digest, stable_json
from .models import (
    ClaimsDecision,
    EvidenceRef,
    GateVote,
    JudgeRecord,
    ModelRole,
    Stage,
    StepJudgment,
)

Judgment = GateVote | ClaimsDecision | StepJudgment
_ROLES = ("gpt", "claude", "gemini")
_STAGES = ("todo_gate", "claims", "step")
_TRANSIENT = {408, 425, 429, 500, 502, 503, 504}
_GATE_SOURCE_KINDS = {
    "context": "incident_context",
    "requirements": "task_requirements",
    "todo": "initial_plan",
}
_REPAIR = (
    "The previous response failed output validation. Return a fresh JSON object "
    "matching the supplied schema, using only exact quotes and IDs from the supplied "
    "sources. Use only the permitted enum values and numeric scores. Do not explain "
    "the correction or invent missing evidence."
)


class JudgeError(RuntimeError):
    """A judge could not produce an approved, input-bound, valid judgment."""


class _InvalidOutput(ValueError):
    pass


class _BorrowedTransport(httpx.BaseTransport):
    """The caller owns an injected transport, including its close lifecycle."""

    def __init__(self, transport: httpx.BaseTransport) -> None:
        self.transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self.transport.handle_request(request)


@dataclass(frozen=True)
class _Request:
    role: ModelRole
    stage: Stage
    step_id: str | None
    model: str
    protocol: str
    prompt: str
    prompt_sha256: str
    schema: dict[str, Any]
    payload: dict[str, Any]
    sources: dict[str, tuple[str, ...]]
    user_content: str
    request_sha256: str


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_constant(value: str) -> None:
    raise _InvalidOutput("Non-finite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidOutput("Duplicate JSON member")
        result[key] = value
    return result


def _json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise _InvalidOutput("Invalid JSON object") from None
    if not isinstance(value, dict):
        raise _InvalidOutput("Expected a JSON object")
    return value


def _output_schema(role: ModelRole, stage: Stage) -> dict[str, Any]:
    contract = {"todo_gate": GateVote, "claims": ClaimsDecision, "step": StepJudgment}[stage]
    schema = contract.model_json_schema()
    if stage == "step":
        properties = schema["properties"]
        properties["faithfulness"] = {"type": "number", "enum": [0, 0.5, 1]}
        if role == "gpt":
            properties["source_trust"] = {"type": "number", "enum": [0, 0.5, 1]}
        else:
            schema["properties"] = {
                name: properties[name] for name in ("faithfulness", "rationale", "references")
            }
            schema.get("$defs", {}).pop("ClaimSupport", None)

    def strict(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for child in node.values():
                strict(child)
        elif isinstance(node, list):
            for child in node:
                strict(child)

    strict(schema)
    return schema


def _sources(payload: dict[str, Any], stage: Stage) -> dict[str, tuple[str, ...]]:
    """Only explicit source fields are citeable; never recursively discover IDs."""
    sources: dict[str, tuple[str, ...]] = {}

    def add(source_id: Any, texts: list[Any]) -> None:
        if not isinstance(source_id, str) or not source_id.strip() or source_id in sources:
            raise JudgeError("Payload has an invalid or ambiguous source ID")
        if any(not isinstance(text, str) for text in texts):
            raise JudgeError("Payload source content must be text")
        sources[source_id] = tuple(texts)

    for name in ("context", "task_instructions", "requirements"):
        if name in payload:
            add(name, [payload[name]])
    if "task_instructions" in payload:
        if "requirements" not in sources:
            add("requirements", [payload["task_instructions"]])
        elif payload["task_instructions"] not in sources["requirements"]:
            raise JudgeError("Payload requirements conflict with task instructions")
    todo = payload.get("todo")
    if todo is not None:
        if not isinstance(todo, dict):
            raise JudgeError("Payload todo must be an object")
        texts: list[str] = []
        if "raw" in todo:
            if not isinstance(todo["raw"], str):
                raise JudgeError("Payload todo raw content must be text")
            texts.append(todo["raw"])
        steps = todo.get("steps", [])
        if not isinstance(steps, list):
            raise JudgeError("Payload todo steps must be a list")
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("id"), str):
                raise JudgeError("Payload todo has an invalid step")
            step_texts = [step[field] for field in ("title", "condition") if field in step]
            add(f"todo:{step['id']}", step_texts)
            texts.extend(step_texts)
        add("todo", texts)
    evidence = payload.get("evidence", [])
    if not isinstance(evidence, list):
        raise JudgeError("Payload evidence must be a list")
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict):
            raise JudgeError("Payload evidence item must be an object")
        source_id = item.get("id")
        if not isinstance(source_id, str) or not source_id.strip() or source_id in seen:
            raise JudgeError("Payload has an invalid or ambiguous source ID")
        seen.add(source_id)
        eligible = item.get("eligible", True)
        if type(eligible) is not bool:
            raise JudgeError("Payload evidence eligibility must be boolean")
        if stage == "todo_gate" and (
            source_id not in _GATE_SOURCE_KINDS
            or item.get("source_kind") != _GATE_SOURCE_KINDS[source_id]
        ):
            continue
        if eligible:
            content = item.get("content")
            # Bundles repeat canonical initial sources in their evidence list.
            if source_id in _GATE_SOURCE_KINDS and source_id in sources:
                if not isinstance(content, str) or content not in sources[source_id]:
                    raise JudgeError("Payload evidence conflicts with an initial source")
            else:
                add(source_id, [content])
    return sources


class JudgeService:
    """Synchronous, thread-safe runtime judges; no tool execution or discovery.

    Executor payloads carry ``case_id``, ``data_sha256``, and ``evidence``
    (EvidenceItem dictionaries). Gate input also carries ``initial_plan``;
    only eligible context/requirements/todo evidence is visible or citeable.
    Claims input carries ``response_text``, ``steps`` and ``calls``; step input
    carries ``step``, assigned ``claims``, selected ``calls`` and documents.
    Claims must quote ``response_text`` exactly.

    Direct ``context``, ``task_instructions`` and ``todo`` (a TodoPlan dictionary)
    fields are also accepted. Their reserved reference IDs are ``context``,
    ``task_instructions`` (also ``requirements``), ``todo``, and ``todo:<step_id>``.
    Identical canonical context/requirements/todo evidence copies are accepted.
    Gate requests expose
    only the initial context, instructions and todo, even if more data is passed.
    ``request_key`` produces the binding required in non-synthetic replay entries.
    """

    def __init__(
        self,
        config: EvaluationConfig,
        cache_dir: Path,
        mode: str = "replay",
        replay: dict | None = None,
        allow_unbound_replay: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if mode not in ("live", "replay"):
            raise JudgeError("Judge mode must be live or replay")
        if allow_unbound_replay and mode != "replay":
            raise JudgeError("Unbound replay is only permitted in synthetic replay mode")
        if replay is not None and not isinstance(replay, dict):
            raise JudgeError("Replay entries must be an object")
        self.config = config.model_copy(deep=True)
        self.cache_dir = Path(cache_dir)
        self.mode: Literal["live", "replay"] = "live" if mode == "live" else "replay"
        self.replay = copy.deepcopy(replay) if replay is not None else {}
        self.allow_unbound_replay = allow_unbound_replay
        self.transport = transport
        self.records: list[JudgeRecord] = []
        self._lock = threading.Lock()
        self._request_locks: dict[str, threading.Lock] = {}

    def _endpoint(self, role: ModelRole) -> ModelEndpoint | None:
        endpoint = self.config.models.get(role)
        if endpoint is None:
            if self.mode == "replay" and self.allow_unbound_replay:
                return None
            raise JudgeError(f"No approved model configuration for role {role}")
        if endpoint.approved_for_incident_data is not True:
            raise JudgeError(f"Role {role} is not approved for incident data")
        model = endpoint.model
        if (
            not model.strip()
            or model != model.strip()
            or re.search(r"[<>{}\s]", model)
            or re.search(
                r"(?i)(placeholder|replace[-_]?me|change[-_]?me|your[-_]|^todo$|^tbd$|^example|^synthetic-)",
                model,
            )
        ):
            raise JudgeError(f"Role {role} requires an explicit non-placeholder model")
        host = endpoint.allowed_host
        if (
            not host
            or host != host.strip()
            or any(char in host for char in "/\\@:*?#%")
            or any(char.isspace() for char in host)
        ):
            raise JudgeError(f"Role {role} requires an exact allowed host")
        if not endpoint.endpoint_env or not endpoint.auth_env:
            raise JudgeError(f"Role {role} requires endpoint and auth environment names")
        return endpoint

    def _prepare(
        self, role: ModelRole, stage: Stage, payload: dict, step_id: str | None
    ) -> _Request:
        if role not in _ROLES or stage not in _STAGES:
            raise JudgeError("Unknown judge role or stage")
        if stage == "claims" and role != "gpt":
            raise JudgeError("Only GPT may extract claims")
        if stage == "step":
            if not isinstance(step_id, str) or not step_id.strip():
                raise JudgeError("Step judgments require a step_id")
        elif step_id is not None:
            raise JudgeError("Only step judgments accept a step_id")
        if not isinstance(payload, dict):
            raise JudgeError("Judge payload must be an object")
        try:
            payload = _json_object(stable_json(payload))
        except (TypeError, ValueError, RecursionError):
            raise JudgeError("Judge payload must be finite JSON data") from None
        endpoint = self._endpoint(role)
        model = endpoint.model if endpoint else f"synthetic-{role}"
        protocol = endpoint.protocol if endpoint else "synthetic"
        try:
            directory = files("sre_assurance").joinpath("prompts")
            prompt = "\n\n".join(
                directory.joinpath(name).read_text(encoding="utf-8")
                for name in ("common.md", f"{stage}_{role}.md")
            )
        except OSError:
            raise JudgeError("Packaged judge prompt is unavailable") from None
        schema = _output_schema(role, stage)
        sources = _sources(payload, stage)
        if stage == "todo_gate":
            visible = {
                name: payload[name]
                for name in (
                    "case_id", "data_sha256", "initial_plan", "context",
                    "task_instructions", "requirements", "todo",
                )
                if name in payload
            }
            visible["evidence"] = [
                {"id": item["id"], "source_kind": item["source_kind"], "content": item["content"]}
                for item in payload.get("evidence", [])
                if item.get("eligible", True)
                and item["id"] in _GATE_SOURCE_KINDS
                and item.get("source_kind") == _GATE_SOURCE_KINDS[item["id"]]
            ]
        else:
            visible = payload
        content = stable_json(
            {
                "untrusted_input": visible,
                "allowed_reference_sources": sources,
                "trust_policy": (
                    [rule.model_dump(mode="json") for rule in self.config.trust_rules]
                    if stage == "step" and role == "gpt" else []
                ),
            }
        )
        prompt_hash = _sha_text(prompt)
        key = digest(
            {
                "version": 2,
                "payload": payload,
                "role": role,
                "stage": stage,
                "step_id": step_id,
                "model": model,
                "protocol": protocol,
                "prompt_sha256": prompt_hash,
                "schema": schema,
                "configuration": self.config.model_dump(mode="json"),
                "inference": {
                    "stream": False,
                    "store": False,
                    "tools": [],
                    "repair_prompt": _REPAIR,
                    "allow_unbound_replay": self.allow_unbound_replay,
                },
            }
        )
        return _Request(
            role, stage, step_id, model, protocol, prompt, prompt_hash, schema, payload,
            sources, content, key,
        )

    def request_key(
        self, role: ModelRole, stage: Stage, payload: dict, step_id: str | None = None
    ) -> str:
        """Return the SHA-256 binding without reading secrets or making requests."""
        return self._prepare(role, stage, payload, step_id).request_sha256

    def judge(
        self, role: ModelRole, stage: Stage, payload: dict, step_id: str | None = None
    ) -> Judgment:
        request = self._prepare(role, stage, payload, step_id)
        with self._lock:
            request_lock = self._request_locks.setdefault(request.request_sha256, threading.Lock())
        with request_lock:
            if self.mode == "replay":
                output = self._replay(request)
                mode: Literal["live", "replay", "cache"] = "replay"
            else:
                cached = self._cache_read(request)
                if cached is not None:
                    output = cached
                    mode = "cache"
                else:
                    output = self._live(request)
                    mode = "live"
            record = JudgeRecord(
                role=role,
                stage=stage,
                step_id=step_id,
                model=request.model,
                request_sha256=request.request_sha256,
                prompt_sha256=request.prompt_sha256,
                mode=mode,
                output=output.model_dump(mode="json", exclude_unset=True),
            )
            if mode == "live":
                self._cache_write(request, record)
            with self._lock:
                self.records.append(record.model_copy(deep=True))
            return output

    def _validate(self, request: _Request, raw: Any) -> Judgment:
        try:
            if not isinstance(raw, dict):
                raise _InvalidOutput("Output must be an object")
            if request.stage == "todo_gate":
                result: Judgment = GateVote.model_validate(raw, strict=True)
                refs = result.references
                reasons = [result.rationale]
                if result.decision != "INSUFFICIENT_EVIDENCE" and not refs:
                    raise _InvalidOutput("A conclusive gate vote requires source references")
            elif request.stage == "claims":
                result = ClaimsDecision.model_validate(raw, strict=True)
                response = request.payload.get("response_text")
                if not isinstance(response, str):
                    raise _InvalidOutput("Claims require response_text")
                if any(not claim.quote.strip() or claim.quote not in response for claim in result.claims):
                    raise _InvalidOutput("A claim quote is not in the response")
                refs = [ref for binding in result.bindings for ref in binding.condition_evidence]
                reasons = [binding.rationale for binding in result.bindings]
            else:
                if request.role == "gpt":
                    if not {"source_trust", "claim_support"}.issubset(raw):
                        raise _InvalidOutput("GPT requires source_trust and claim_support")
                elif set(raw) - {"faithfulness", "rationale", "references"}:
                    raise _InvalidOutput("Only GPT may judge claims or source trust")
                result = StepJudgment.model_validate(raw, strict=True)
                refs = result.references + [ref for support in result.claim_support for ref in support.references]
                reasons = [result.rationale] + [support.rationale for support in result.claim_support]
                if any(
                    support.verdict != "UNSUPPORTED" and not support.references
                    for support in result.claim_support
                ):
                    raise _InvalidOutput("A claim support verdict requires source references")
                if request.role == "gpt":
                    if result.source_trust is None:
                        raise _InvalidOutput("GPT source trust must be a numeric tri score")
                    reasons.append(result.trust_rationale)
                    rules = {rule.id: rule for rule in self.config.trust_rules}
                    if len(set(result.trust_policy_ids)) != len(result.trust_policy_ids):
                        raise _InvalidOutput("Duplicate trust policy IDs")
                    if any(rule_id not in rules for rule_id in result.trust_policy_ids):
                        raise _InvalidOutput("Unknown trust policy ID")
                    if result.source_trust > 0 and not result.trust_policy_ids:
                        raise _InvalidOutput("Positive trust requires an explicit policy rule")
                    for rule_id in result.trust_policy_ids:
                        rule = rules[rule_id]
                        if result.source_trust > rule.maximum_score:
                            raise _InvalidOutput("Trust exceeds the cited policy ceiling")
                        if not any(
                            item.get("eligible", True)
                            and item.get("source_kind") == rule.source_kind
                            and isinstance(item.get("origin"), str)
                            and item["origin"].startswith(rule.origin_prefix)
                            for item in request.payload.get("evidence", [])
                        ):
                            raise _InvalidOutput("Trust rule does not match supplied evidence")
            if any(not reason.strip() or len(reason) > 1200 for reason in reasons):
                raise _InvalidOutput("Reasons must contain 1 to 1200 characters")
            self._validate_refs(request, refs)
            return result
        except (ValidationError, _InvalidOutput):
            raise _InvalidOutput("Output failed schema or evidence validation") from None

    @staticmethod
    def _validate_refs(request: _Request, refs: list[EvidenceRef]) -> None:
        for ref in refs:
            texts = request.sources.get(ref.evidence_id, ())
            if not ref.quote.strip() or not any(ref.quote in text for text in texts):
                raise _InvalidOutput("Reference ID or exact quote is not in allowed sources")

    def _replay(self, request: _Request) -> Judgment:
        key = (
            f"step:{request.step_id}:{request.role}"
            if request.stage == "step"
            else f"{request.stage}:{request.role}"
        )
        entry = self.replay.get(key)
        if not isinstance(entry, dict) or "output" not in entry:
            raise JudgeError(f"Missing replay entry for {key}")
        if "model" not in entry:
            if not self.allow_unbound_replay:
                raise JudgeError(f"Replay model metadata is required for {key}")
        elif entry["model"] != request.model:
            raise JudgeError(f"Replay model mismatch for {key}")
        if "request_sha256" not in entry:
            if not self.allow_unbound_replay:
                raise JudgeError(f"Replay request binding is required for {key}")
        elif entry["request_sha256"] != request.request_sha256:
            raise JudgeError(f"Replay request binding mismatch for {key}")
        try:
            return self._validate(request, entry["output"])
        except _InvalidOutput:
            raise JudgeError(f"Invalid replay output for {key}") from None

    def _cache_read(self, request: _Request) -> Judgment | None:
        path = self.cache_dir / f"{request.request_sha256}.json"
        try:
            envelope = _json_object(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, _InvalidOutput):
            raise JudgeError("Judge cache is unreadable or invalid") from None
        try:
            if set(envelope) != {"record", "output_sha256"}:
                raise _InvalidOutput("Invalid cache envelope")
            record = JudgeRecord.model_validate(envelope["record"], strict=True)
            if (
                record.role != request.role
                or record.stage != request.stage
                or record.step_id != request.step_id
                or record.model != request.model
                or record.request_sha256 != request.request_sha256
                or record.prompt_sha256 != request.prompt_sha256
                or record.mode != "live"
                or digest(record.output) != envelope["output_sha256"]
            ):
                raise _InvalidOutput("Cache integrity mismatch")
            return self._validate(request, record.output)
        except (ValidationError, ValueError, TypeError):
            raise JudgeError("Judge cache failed integrity or output validation") from None

    def _cache_write(self, request: _Request, record: JudgeRecord) -> None:
        temporary: Path | None = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.cache_dir, prefix=".judge-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(stable_json({"record": record.model_dump(mode="json"), "output_sha256": digest(record.output)}))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.cache_dir / f"{request.request_sha256}.json")
        except OSError:
            raise JudgeError("Could not persist the validated judge cache") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise JudgeError("Could not clean up the temporary judge cache") from None

    def _live_endpoint(self, request: _Request) -> tuple[str, dict[str, str]]:
        endpoint = self._endpoint(request.role)
        if endpoint is None:
            raise JudgeError("Synthetic models cannot make live requests")
        url = os.environ.get(endpoint.endpoint_env, "")
        secret = os.environ.get(endpoint.auth_env, "")
        try:
            parsed = urlsplit(url)
            valid = (
                url == url.strip()
                and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
                and "\\" not in url
                and parsed.scheme == "https"
                and parsed.hostname is not None
                and parsed.hostname.lower() == endpoint.allowed_host.lower()
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
            )
            # Accessing port validates malformed/out-of-range ports without rewriting the URL.
            _ = parsed.port
            httpx_url = httpx.URL(url)
            valid = valid and httpx_url.scheme == "https" and httpx_url.host == endpoint.allowed_host.lower()
        except (ValueError, httpx.InvalidURL):
            valid = False
        if not valid:
            raise JudgeError("Judge endpoint must be exact approved HTTPS without credentials or redirects")
        if not secret or not secret.strip() or any(ord(char) < 32 or ord(char) >= 127 for char in secret):
            raise JudgeError("Judge authentication is missing or invalid")
        authorization = f"Bearer {secret}" if endpoint.auth_header == "Authorization" else secret
        return url, {endpoint.auth_header: authorization, "Content-Type": "application/json"}

    def _body(self, request: _Request, repair: bool) -> dict[str, Any]:
        prompt = request.prompt + ("\n\n" + _REPAIR if repair else "")
        format_spec = {"name": f"{request.stage}_{request.role}", "strict": True, "schema": request.schema}
        if request.protocol == "openai_chat":
            return {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": request.user_content},
                ],
                "response_format": {"type": "json_schema", "json_schema": format_spec},
                "stream": False,
                "store": False,
            }
        if request.protocol == "openai_responses":
            return {
                "model": request.model,
                "instructions": prompt,
                "input": [{"role": "user", "content": request.user_content}],
                "text": {"format": {"type": "json_schema", **format_spec}},
                "tools": [],
                "stream": False,
                "store": False,
            }
        raise JudgeError("Unsupported judge protocol; no fallback is permitted")

    @staticmethod
    def _response_output(response: httpx.Response, request: _Request) -> dict[str, Any]:
        envelope = _json_object(response.text)
        if envelope.get("model") is not None and envelope["model"] != request.model:
            raise JudgeError("Judge returned a different model; no fallback is permitted")
        if "error" in envelope:
            raise JudgeError("Judge returned a provider error")
        if request.protocol == "openai_chat":
            choices = envelope.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise _InvalidOutput("Expected one chat completion")
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, dict):
                raise _InvalidOutput("Chat completion has no message")
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise JudgeError("Judge refused the request")
            if message.get("tool_calls") or message.get("function_call"):
                raise JudgeError("Judge attempted a prohibited tool request")
            if choice.get("finish_reason") == "length":
                raise JudgeError("Judge output budget was exhausted")
            if choice.get("finish_reason") != "stop":
                raise _InvalidOutput("Chat completion did not finish normally")
            text = message.get("content")
        else:
            if envelope.get("status") == "incomplete":
                raise JudgeError("Judge response was incomplete or exhausted its output budget")
            if envelope.get("status") not in (None, "completed"):
                raise JudgeError("Judge response did not complete")
            output = envelope.get("output")
            if not isinstance(output, list):
                raise _InvalidOutput("Responses output must be a list")
            texts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    raise _InvalidOutput("Malformed response item")
                if item.get("type") == "reasoning":
                    continue
                if item.get("type") != "message":
                    raise JudgeError("Judge attempted a prohibited tool or autonomous request")
                content = item.get("content")
                if not isinstance(content, list):
                    raise _InvalidOutput("Malformed response message")
                for part in content:
                    if not isinstance(part, dict):
                        raise _InvalidOutput("Malformed response content")
                    if part.get("type") == "refusal":
                        raise JudgeError("Judge refused the request")
                    if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                        raise _InvalidOutput("Expected JSON text")
                    texts.append(part["text"])
            if len(texts) != 1:
                raise _InvalidOutput("Expected one JSON response")
            text = texts[0]
        if not isinstance(text, str):
            raise _InvalidOutput("Judge output must be JSON text")
        return _json_object(text)

    def _live(self, request: _Request) -> Judgment:
        url, headers = self._live_endpoint(request)
        repair = False
        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            transport=_BorrowedTransport(self.transport) if self.transport is not None else None,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for attempt in range(self.config.max_attempts):
                body = stable_json(self._body(request, repair))
                if len(body) > self.config.max_request_characters:
                    raise JudgeError("Judge request exceeds max_request_characters budget")
                try:
                    response = client.post(url, headers=headers, content=body.encode("utf-8"))
                except httpx.TransportError:
                    if attempt + 1 == self.config.max_attempts:
                        raise JudgeError("Judge transport failed within the attempt budget") from None
                    time.sleep(min(0.1 * (2 ** attempt), 1.0))
                    continue
                if response.status_code in (401, 403):
                    raise JudgeError("Judge authentication or authorization failed")
                if response.status_code in _TRANSIENT:
                    if attempt + 1 == self.config.max_attempts:
                        raise JudgeError("Judge transient failure exhausted the attempt budget")
                    time.sleep(min(0.1 * (2 ** attempt), 1.0))
                    continue
                if not 200 <= response.status_code < 300:
                    raise JudgeError(f"Judge HTTP request failed with status {response.status_code}")
                try:
                    output = self._validate(request, self._response_output(response, request))
                    auth_value = next(value for key, value in headers.items() if key != "Content-Type")
                    secret = auth_value.removeprefix("Bearer ") if "Authorization" in headers else auth_value
                    if secret in stable_json(output.model_dump(mode="json")):
                        raise JudgeError("Judge output contains authentication data")
                    return output
                except _InvalidOutput:
                    if repair or attempt + 1 == self.config.max_attempts:
                        raise JudgeError("Judge output invalid within the schema repair/attempt budget") from None
                    repair = True
        raise JudgeError("Judge attempt budget exhausted")
