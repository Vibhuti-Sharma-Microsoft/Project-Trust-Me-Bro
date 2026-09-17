from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

from .config import digest
from .imports import ImportFailure, file_hash, load_table, safe_path
from .models import CaseSpec, EvidenceItem, ResponseBundle, TodoPlan, TodoStep, ToolCall
from .time_utils import timestamp_ns


def payload_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def parsed_input(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def payload_flags(raw: str) -> list[str]:
    flags = []
    if raw.strip().lower() in ("", "null"):
        flags.append("MISSING")
    if re.search(r"<redacted[:\-]|\\u003credacted", raw, re.IGNORECASE):
        flags.append("REDACTED")
    if "contentPreview" in raw:
        flags.append("PREVIEW")
    if re.search(r'(?:\.{3}|\u2026)\s*["\]}]*$', raw.strip()):
        flags.append("POSSIBLY_SHORTENED")
    if 8000 <= len(raw) <= 8192:
        flags.append("SIZE_BOUNDARY_INDICATOR")
    return flags


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.suppressed += 1
        if not self.suppressed and tag in {"br", "p", "div", "h1", "h2", "h3", "tr", "li", "pre"}:
            self.parts.append("\n")
        if not self.suppressed and tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.suppressed:
            self.suppressed -= 1
        if not self.suppressed and tag in {"p", "div", "h1", "h2", "h3", "tr", "li", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def response_text(raw: str, is_html: bool) -> str:
    if not is_html:
        return raw
    parser = _VisibleText()
    parser.feed(raw)
    return re.sub(r"\n[ \t]*\n+", "\n", "".join(parser.parts)).strip()


def _normalized_post_text(text: str) -> str:
    if re.search(r"</?(?:div|p|table|h[1-6]|a|pre)\b", text, re.IGNORECASE):
        text = response_text(text, True)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*(?:[-+*]|\d+[.)])\s+", "", text)
    text = re.sub(r"(?m)^\s*\|?\s*:?-{3,}[-:|\s]*$", "", text)
    for token in ("```", "`", "**", "__"):
        text = text.replace(token, "")
    return " ".join(text.replace("|", " ").split())


def _validate_post_body(arguments: dict[str, Any], selected_text: str, warnings: list[str]) -> None:
    body = arguments.get("discussionEntry")
    if not isinstance(body, str) or not body.strip():
        raise ImportFailure("Mapped posting arguments do not contain a readable discussionEntry")
    flags = payload_flags(body)
    actual = _normalized_post_text(selected_text)
    if flags:
        warnings.append("POSTING_BODY_PARTIAL: external mapping verification is required for unavailable text")
        segments = re.split(r"<redacted[^>]*>|\.\.\.|\u2026", body, flags=re.IGNORECASE)
        position = 0
        for segment in segments:
            normalized = _normalized_post_text(segment)
            if len(normalized) < 20:
                continue
            found = actual.find(normalized, position)
            if found < 0:
                raise ImportFailure("Readable posting-body fragment does not match the selected response")
            position = found + len(normalized)
    elif _normalized_post_text(body) not in actual:
        raise ImportFailure("Selected response does not match the mapped posting discussionEntry")
    query = arguments.get("kustoQuery")
    if isinstance(query, str) and query.strip() and not payload_flags(query):
        if " ".join(query.split()) not in " ".join(selected_text.split()):
            raise ImportFailure("Selected response omits or changes its posted Kusto attachment")


def _row_id(row: dict[str, Any]) -> str:
    native = row.get("itemId")
    return f"{row['_table']}:{native}" if native else f"{row['_table']}:local:{digest(row)}"


def _thread_matches(row: dict[str, Any], thread: str) -> bool:
    cd = row["customDimensions"]
    threads = {str(cd[key]) for key in ("ThreadId", "thread_id", "ChatThreadId") if cd.get(key)}
    return bool(threads) and threads == {thread}


def _traces(row: dict[str, Any]) -> set[str]:
    return {str(value) for value in (row.get("operation_Id"), row["customDimensions"].get("TraceId")) if value}


def _source(call: ToolCall) -> tuple[str, str, str]:
    args = call.input or {}
    if args.get("cluster") and args.get("database") and args.get("fullQuery"):
        return "kusto", f"{args['cluster']}/{args['database']}", str(args["fullQuery"])
    if args.get("incidentId") is not None:
        return "incident", f"icm:{args['incidentId']}", ""
    for key in ("url", "uri", "documentUrl", "filePath", "file_path", "path", "skillName"):
        if args.get(key):
            return "document", str(args[key]), ""
    if any(token in call.name.lower() for token in ("skill", "document", "knowledge", "readfile")):
        return "document", f"unresolved:{call.name}", ""
    return "other", f"tool:{call.name}", ""


def _todo_from_call(call: ToolCall) -> TodoPlan | None:
    if call.name != "ManageTodoList" or call.status != "PAIRED" or call.started_at is None:
        return None
    if call.input is None or any(flag in payload_flags(call.input_raw) for flag in ("REDACTED", "PREVIEW", "POSSIBLY_SHORTENED", "SIZE_BOUNDARY_INDICATOR")):
        return None
    raw_steps = call.input.get("todoList", call.input.get("todos", call.input.get("todo_list")))
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    steps = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            return None
        title = item.get("title", item.get("description"))
        if not isinstance(title, str) or not title.strip():
            return None
        if str(item.get("status", "")).lower() in {"completed", "complete", "done"}:
            return None
        kind: Literal["evidence", "housekeeping", "conditional"] = "evidence"
        if re.match(r"(?i)^(wait|post|publish|update (?:the )?(?:todo|task))\b", title):
            kind = "housekeeping"
        elif re.match(r"(?i)^(if|when|for each)\b", title):
            kind = "conditional"
        steps.append(TodoStep(id=f"step-{item.get('id', index + 1)}", title=title, kind=kind,
                              condition=title if kind == "conditional" else ""))
    if len({step.id for step in steps}) != len(steps):
        return None
    return TodoPlan(source_call_id=call.id, created_at=call.started_at, steps=steps, raw=call.input_raw)


def build_bundle(case: CaseSpec, corpus_root: Path) -> ResponseBundle:
    if not case.mapping_verified:
        raise ImportFailure("The response-to-posting-call mapping has not been verified")
    cutoff = timestamp_ns(case.cutoff)
    response_path = safe_path(corpus_root, case.response_path)
    if file_hash(response_path) != case.response_sha256:
        raise ImportFailure("Response content hash does not match the selected version")
    raw_response = response_path.read_bytes().decode("utf-8-sig")
    text = response_text(raw_response, response_path.suffix.lower() in {".html", ".htm"})
    if not text.strip():
        raise ImportFailure("Selected response is empty")
    context_path = safe_path(corpus_root, case.context_path)
    context = context_path.read_text(encoding="utf-8-sig")
    warnings: list[str] = []
    if not case.context_available_at:
        warnings.append("CONTEXT_TIME_UNVERIFIED")
    elif timestamp_ns(case.context_available_at) >= cutoff:
        warnings.append("CONTEXT_AFTER_CUTOFF")
    rows: list[dict[str, Any]] = []
    hashes = {"response": case.response_sha256, "context": file_hash(context_path)}
    for table, relative in case.log_files.items():
        path = safe_path(corpus_root, relative)
        table_rows = load_table(path, table)
        if table in case.expected_rows and len(table_rows) != case.expected_rows[table]:
            raise ImportFailure(f"{table}: raw row count does not match the export manifest")
        if table not in case.expected_rows:
            warnings.append(f"EXPORT_COUNT_UNVERIFIED:{table}")
        rows.extend(table_rows)
        hashes[table] = file_hash(path)
    if "customEvents" not in case.log_files:
        raise ImportFailure("customEvents raw records are required")
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        timestamp_ns(str(row["timestamp"]))
        key = _row_id(row)
        if key in unique:
            old = {k: v for k, v in unique[key].items() if k != "_local_row"}
            new = {k: v for k, v in row.items() if k != "_local_row"}
            if old != new:
                raise ImportFailure(f"Conflicting copies of telemetry record {key}")
        unique[key] = row
    rows = list(unique.values())
    anchors = [
        row for row in rows
        if row["_table"] == "customEvents" and row.get("name") == "AgentToolExecution"
        and _thread_matches(row, case.thread_id)
        and row["customDimensions"].get("CallId") == case.post_call_id
        and row["customDimensions"].get("ToolName") == "PostDiscussionEntry"
        and row["customDimensions"].get("EventType") == "ToolStart"
    ]
    if len(anchors) != 1:
        raise ImportFailure("Expected exactly one mapped posting ToolStart")
    anchor = anchors[0]
    if timestamp_ns(str(anchor["timestamp"])) != cutoff:
        raise ImportFailure("Mapped posting cutoff does not match the log record")
    anchor_input = parsed_input(payload_text(anchor["customDimensions"].get("ToolInput")))
    if anchor_input is None or str(anchor_input.get("incidentId")) != case.incident_id:
        raise ImportFailure("Mapped posting arguments do not identify the selected incident")
    _validate_post_body(anchor_input, text, warnings)
    traces = _traces(anchor)
    if not traces:
        raise ImportFailure("Posting call lacks trace context")
    if len(traces) != 1:
        raise ImportFailure("Posting native and custom trace IDs disagree")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cd = row["customDimensions"]
        if row["_table"] != "customEvents" or row.get("name") != "AgentToolExecution":
            continue
        if not _thread_matches(row, case.thread_id):
            continue
        call_id = str(cd.get("CallId") or "")
        if not (_traces(row) & traces) and call_id not in case.carry_forward_call_ids:
            continue
        if timestamp_ns(str(row["timestamp"])) >= cutoff and call_id != case.post_call_id:
            continue
        if cd.get("EventType") not in {"ToolStart", "ToolEnd"}:
            continue
        grouped[call_id or f"unlinked:{_row_id(row)}"].append(row)
    calls = []
    evidence = [
        EvidenceItem(id="context", source_kind="incident_context", origin=f"icm:{case.incident_id}",
                     content=context, observed_at=case.context_available_at),
        EvidenceItem(id="requirements", source_kind="task_requirements", origin="case-manifest",
                     content=case.task_instructions, eligible=bool(case.task_instructions)),
    ]
    for call_id, events in grouped.items():
        starts = [row for row in events if row["customDimensions"].get("EventType") == "ToolStart"]
        ends = [row for row in events if row["customDimensions"].get("EventType") == "ToolEnd"]
        names = {str(row["customDimensions"].get("ToolName", "")) for row in events}
        start = starts[0] if len(starts) == 1 else None
        end = ends[0] if len(ends) == 1 else None
        status: Literal["PAIRED", "ORPHAN", "DUPLICATE", "AFTER_CUTOFF", "CONFLICT"] = "PAIRED"
        if call_id.startswith("unlinked:") or not starts or not ends:
            status = "ORPHAN"
        elif len(starts) > 1 or len(ends) > 1:
            status = "DUPLICATE"
        elif len(names) != 1 or any(len(_traces(row)) > 1 for row in events) or (start and end and timestamp_ns(str(end["timestamp"])) < timestamp_ns(str(start["timestamp"]))):
            status = "CONFLICT"
        input_raw = payload_text(start["customDimensions"].get("ToolInput")) if start else ""
        output_raw = payload_text(end["customDimensions"].get("ToolOutput")) if end else ""
        call = ToolCall(
            id=call_id, name=next(iter(names)) if len(names) == 1 else "AMBIGUOUS",
            thread_id=case.thread_id, trace_id=next(iter(traces)),
            started_at=str(start["timestamp"]) if start else None,
            completed_at=str(end["timestamp"]) if end else None,
            input_raw=input_raw, output_raw=output_raw, input=parsed_input(input_raw),
            start_record_ids=[_row_id(row) for row in starts], end_record_ids=[_row_id(row) for row in ends],
            status=status, quality_flags=payload_flags(output_raw),
        )
        calls.append(call)
        if call.id == case.post_call_id or call.name == "PostDiscussionEntry":
            continue
        kind, origin, query = _source(call)
        eligible = call.status == "PAIRED" and call.completed_at is not None and timestamp_ns(call.completed_at) < cutoff
        if call.name in {"ManageTodoList", "WaitInMilliSeconds"}:
            kind = "housekeeping"
        evidence.append(EvidenceItem(
            id=f"tool:{call.id}", source_kind=kind, origin=origin, content=call.output_raw,
            call_id=call.id, source_record_id=",".join(call.end_record_ids),
            completed_at=call.completed_at, query=query, quality_flags=call.quality_flags,
            eligible=eligible and kind != "housekeeping" and "MISSING" not in call.quality_flags,
        ))
    calls.sort(key=lambda call: timestamp_ns(call.started_at or call.completed_at or case.cutoff))
    todo_calls = [call for call in calls if call.name == "ManageTodoList"]
    # An unreadable initial plan is not rescued by a later, better plan.
    todo = _todo_from_call(todo_calls[0]) if todo_calls else None
    if todo is None:
        warnings.append("INITIAL_TODO_UNAVAILABLE_OR_UNREADABLE")
    elif case.context_available_at and timestamp_ns(case.context_available_at) > timestamp_ns(todo.created_at):
        warnings.append("CONTEXT_NOT_AVAILABLE_FOR_INITIAL_TODO")
    if todo:
        evidence.append(EvidenceItem(id="todo", source_kind="initial_plan", origin=todo.source_call_id,
                                    content=todo.raw, observed_at=todo.created_at))
    content_ids = {
        str(row["customDimensions"].get("_MS.GenAIContentId"))
        for row in rows if row["_table"] == "dependencies" and _traces(row) & traces
        and timestamp_ns(str(row["timestamp"])) < cutoff and row["customDimensions"].get("_MS.GenAIContentId")
    }
    for row in rows:
        if timestamp_ns(str(row["timestamp"])) >= cutoff or row["_table"] == "customEvents":
            continue
        table = str(row["_table"])
        linked_content = table == "genAIContent" and str(row["customDimensions"].get("_MS.GenAIContentId", "")) in content_ids
        if not (_traces(row) & traces or linked_content):
            continue
        explicit_threads = {str(row["customDimensions"][k]) for k in ("ThreadId", "thread_id", "ChatThreadId") if row["customDimensions"].get(k)}
        if explicit_threads and explicit_threads != {case.thread_id}:
            warnings.append(f"EXCLUDED_CROSS_THREAD_RECORD:{_row_id(row)}")
            continue
        if table == "genAIContent":
            content = payload_text(row.get("outputMessages", row.get("OutputMessages", "")))
        else:
            content = payload_text(row.get("message", row.get("outerMessage", row.get("data", ""))))
        if content:
            evidence.append(EvidenceItem(
                id=f"record:{_row_id(row)}", source_kind="diagnostic_context", origin=table,
                content=content, source_record_id=_row_id(row), observed_at=str(row["timestamp"]),
                quality_flags=payload_flags(content), eligible=False,
            ))
    return ResponseBundle(case=case, response_text=text, context=context, todo=todo, calls=calls,
                          evidence=evidence, warnings=warnings,
                          data_sha256=digest({"case": case.model_dump(mode="json", exclude={"replay_path"}), "files": hashes}))
