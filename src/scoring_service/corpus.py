"""Prepare a local collection checklist, without fetching or fabricating incident evidence."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .imports import write_json
from .models import Contract
from .time_utils import timestamp_ns


class KnownResponse(Contract):
    incident_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    message_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    thread_id: str = Field(min_length=1)
    post_call_id: str = Field(min_length=1)
    cutoff: str
    mapping_provenance: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_time(self) -> KnownResponse:
        timestamp_ns(self.cutoff)
        return self


class IncidentRoster(Contract):
    schema_version: Literal["1"] = "1"
    incident_ids: list[str] = Field(min_length=1)
    known_responses: list[KnownResponse] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_roster(self) -> IncidentRoster:
        if any(re.fullmatch(r"[1-9][0-9]{0,19}", value) is None for value in self.incident_ids):
            raise ValueError("Incident IDs must be positive decimal strings")
        if len(self.incident_ids) != len(set(self.incident_ids)):
            raise ValueError("The roster contains duplicate incident IDs")
        mapped = [entry.incident_id for entry in self.known_responses]
        if len(mapped) != len(set(mapped)) or not set(mapped) <= set(self.incident_ids):
            raise ValueError("Known responses must refer to distinct incidents in the roster")
        return self


def _targets(incident_ids: list[str]) -> str:
    values = ",\n    ".join(f'"{value}"' for value in incident_ids)
    return "let Targets = datatable(IncidentId:string)\n[\n    " + values + "\n];\n"


def prepare_corpus(roster_path: Path, directory: Path) -> Path:
    roster = IncidentRoster.model_validate_json(roster_path.read_text(encoding="utf-8-sig"))
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError("Collection destination must be empty; existing data is never overwritten")
    directory.mkdir(parents=True, exist_ok=True)
    mappings = {entry.incident_id: entry.model_dump(mode="json") for entry in roster.known_responses}
    cases = []
    for incident_id in roster.incident_ids:
        prefix = f"incidents/{incident_id}"
        (directory / prefix).mkdir(parents=True)
        cases.append({
            "case_id": f"incident-{incident_id}-first",
            "incident_id": incident_id,
            "state": "AWAITING_EXPORTS",
            "known_response": mappings.get(incident_id),
            "required_files": {
                "response": f"{prefix}/response.html",
                "context": f"{prefix}/context.txt",
                "customEvents": f"{prefix}/customEvents.json",
            },
            "optional_log_files": {
                table: f"{prefix}/{table}.json"
                for table in ("dependencies", "genAIContent", "traces", "requests", "exceptions")
            },
            "remaining": [
                "Verify the first diagnostic message, posting CallId and cutoff",
                "Save the actual response body and any posted query attachment",
                "Save original task context with its availability timestamp",
                "Export raw telemetry including the initial todo and pre-post tool results",
                "Check row counts and retention; record unavailable evidence explicitly",
                "Map referenced document calls to permitted snapshots/versions",
                "Create a valid cases.json manifest and run validate",
            ],
        })
    plan = {
        "schema_version": "collection-plan-v1",
        "ready_for_evaluation": False,
        "target_real_cases": len(roster.incident_ids),
        "note": "This is a collection checklist, NOT an executable CorpusManifest and NOT test results.",
        "cases": cases,
    }
    plan_path = directory / "collection-plan.json"
    write_json(plan_path, plan)
    query_dir = directory / "queries"
    query_dir.mkdir()
    preamble = (
        "// READ ONLY. Run manually in the relevant Application Insights resource.\n"
        "// Align the portal time picker with Lookback; older data may be outside retention.\n"
        "let Lookback = 90d;\n" + _targets(roster.incident_ids)
    )
    discovery = preamble + """let Observed =
    customEvents
    | where timestamp >= ago(Lookback)
    | where name == "IncidentActivitySnapshot"
    | extend IncidentId = tostring(customDimensions.IncidentId),
        ThreadId = coalesce(tostring(customDimensions.ThreadId),
            tostring(customDimensions.ChatThreadId), tostring(customDimensions.thread_id))
    | where IncidentId in (Targets)
    | summarize FirstSeen=min(timestamp), LastSeen=max(timestamp), SnapshotRows=count()
        by IncidentId, ThreadId;
Targets
| join kind=leftouter Observed on IncidentId
| extend CollectionState = iff(isempty(ThreadId), "NO_THREAD_FOUND_IN_SCOPE_OR_WINDOW", "REVIEW_CANDIDATE_THREAD")
| project IncidentId, ThreadId, CollectionState, FirstSeen, LastSeen, SnapshotRows
| order by IncidentId asc, FirstSeen asc
"""
    postings = preamble + """customEvents
| where timestamp >= ago(Lookback)
| where name == "AgentToolExecution"
| where tostring(customDimensions.ToolName) == "PostDiscussionEntry"
    and tostring(customDimensions.EventType) == "ToolStart"
| extend Request = parse_json(tostring(customDimensions.ToolInput))
| extend IncidentId = tostring(Request.incidentId)
| where IncidentId in (Targets)
| project IncidentId, timestamp, itemId,
    ThreadId=coalesce(tostring(customDimensions.ThreadId), tostring(customDimensions.thread_id), tostring(customDimensions.ChatThreadId)),
    PostCallId=tostring(customDimensions.CallId),
    NativeTraceId=operation_Id, CustomTraceId=tostring(customDimensions.TraceId),
    ToolInputRaw=tostring(customDimensions.ToolInput)
| order by IncidentId asc, timestamp asc
// These are posting attempts. Verify the actual IcM message separately.
// The first posting attempt may be an execution plan, not the first diagnostic response.
"""
    (query_dir / "01-discover-threads.kql").write_text(discovery, encoding="utf-8")
    (query_dir / "02-posting-candidates.kql").write_text(postings, encoding="utf-8")
    return plan_path
