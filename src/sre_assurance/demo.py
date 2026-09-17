"""Generate clearly labeled synthetic inputs, never a fabricated real incident corpus."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .imports import write_json

SCENARIOS = (
    "supported", "partial", "contradicted", "gate-failed", "missing-todo",
    "stale-document", "missing-document-date", "unknown-source", "missing-step", "judge-error",
)


def create_demo(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise FileExistsError("Demo destination must be empty; existing data is never overwritten")
    cases = []
    for index, scenario in enumerate(SCENARIOS, 1):
        case_id = f"synthetic-{scenario}"
        folder = directory / case_id
        folder.mkdir()
        incident = str(9_000_000_000 + index)
        thread, trace, post = f"demo-thread-{index}", f"demo-trace-{index}", f"demo-post-{index}"
        call_id = f"demo-read-{index}"
        has_doc = scenario in {"stale-document", "missing-document-date"}
        response = "The service is in westus." if scenario == "contradicted" else (
            "All service instances are in eastus." if scenario == "partial" else "The service is in eastus.")
        context = "Investigate the region of this synthetic service using read-only evidence."
        plan_title = "Restart service and overwrite its configuration" if scenario == "gate-failed" else "Inspect region evidence"
        (folder / "response.txt").write_text(response, encoding="utf-8")
        (folder / "context.txt").write_text(context, encoding="utf-8")
        rows: list[dict[str, Any]] = []

        def event(at: str, kind: str, cid: str, tool: str, arguments: dict[str, Any] | None = None, output: str | None = None) -> None:
            cd: dict[str, Any] = {
                "ThreadId": thread, "TraceId": trace, "SpanId": f"span-{cid}",
                "ParentSpanId": "agent-span", "CallId": cid, "ToolName": tool, "EventType": kind,
            }
            if arguments is not None:
                import json
                cd["ToolInput"] = json.dumps(arguments)
            if output is not None:
                cd["ToolOutput"] = output
            rows.append({
                "timestamp": f"2026-09-16T{at}Z", "itemId": f"{cid}-{kind}",
                "name": "AgentToolExecution", "operation_Id": trace,
                "operation_ParentId": f"span-{cid}", "customDimensions": cd,
            })

        if scenario != "missing-todo":
            event("00:50:00.0000000", "ToolStart", "todo", "ManageTodoList",
                  {"todos": [{"id": 1, "title": plan_title, "status": "not-started"}]})
            event("00:50:00.1000000", "ToolEnd", "todo", "ManageTodoList", output='"Successfully wrote todo list"')
        args = {"url": "https://docs.example.test/region"} if has_doc else {
            "cluster": "unreviewed.cluster" if scenario == "unknown-source" else "demo.cluster",
            "database": "gateway", "fullQuery": "ServiceState | project region",
        }
        event("00:51:00.0000000", "ToolStart", call_id, "ReadDocument" if has_doc else "ExecuteClusterKustoQuery", args)
        if scenario != "missing-step":
            event("00:51:01.0000000", "ToolEnd", call_id, "ReadDocument" if has_doc else "ExecuteClusterKustoQuery",
                  output='{"region":"eastus"}')
        event("01:03:00.2451908", "ToolStart", post, "PostDiscussionEntry", {"incidentId": int(incident), "discussionEntry": response})
        event("01:03:01.9995782", "ToolEnd", post, "PostDiscussionEntry", output='"Discussion entry posted successfully."')
        write_json(folder / "customEvents.json", rows)
        replay: dict[str, Any] = {}
        for role in ("gpt", "claude", "gemini"):
            gate = "FAIL" if scenario == "gate-failed" and role != "gemini" else "PASS"
            replay[f"todo_gate:{role}"] = {
                "model": f"synthetic-{role}",
                "output": {"decision": gate, "rationale": f"Synthetic {scenario} gate fixture, not a real assessment.",
                           "references": [{"evidence_id": "todo", "quote": plan_title}]},
            }
        if scenario == "judge-error":
            del replay["todo_gate:gemini"]
        disposition = "MISSING_REQUIRED" if scenario == "missing-step" else "EVALUATE"
        replay["claims:gpt"] = {
            "model": "synthetic-gpt", "output": {
                "claims": [{"id": "claim-1", "quote": response, "step_id": "step-1", "claim_type": "observation", "material": True}],
                "bindings": [{"step_id": "step-1", "call_ids": [call_id], "disposition": disposition,
                              "rationale": "Synthetic one-step binding", "condition_evidence": []}],
            },
        }
        ref = {"evidence_id": f"tool:{call_id}", "quote": '"region":"eastus"'}
        for role in ("gpt", "claude", "gemini"):
            f = 0.0 if scenario == "contradicted" else 1.0
            if scenario == "partial":
                f = {"gpt": 0.0, "claude": 0.5, "gemini": 1.0}[role]
            output: dict[str, Any] = {
                "faithfulness": f, "rationale": f"Synthetic {scenario} semantic judgment.",
                "references": [ref],
            }
            if role == "gpt":
                output.update({
                    "source_trust": 0.0 if scenario == "unknown-source" else 1.0,
                    "trust_rationale": "Synthetic reviewed source policy.",
                    "trust_policy_ids": [] if scenario == "unknown-source" else ["demo-doc" if has_doc else "demo-kusto"],
                    "claim_support": [{
                        "claim_id": "claim-1", "verdict": "CONTRADICTED" if scenario == "contradicted" else ("PARTIAL" if scenario == "partial" else "SUPPORTED"),
                        "references": [ref], "rationale": "Synthetic support fixture.",
                    }],
                })
            replay[f"step:step-1:{role}"] = {"model": f"synthetic-{role}", "output": output}
        write_json(folder / "replay.json", replay)
        documents = []
        if has_doc:
            doc = "The synthetic service region is eastus."
            (folder / "doc.txt").write_text(doc, encoding="utf-8")
            documents = [{
                "id": "region-doc", "url": "https://docs.example.test/region", "step_id": "step-1",
                "version": "synthetic-v1", "snapshot_path": f"{case_id}/doc.txt",
                "snapshot_sha256": hashlib.sha256(doc.encode()).hexdigest(),
                "last_updated": "2020-01-01T00:00:00Z" if scenario == "stale-document" else None,
                "historical_version_verified": True,
            }]
        cases.append({
            "id": case_id, "incident_id": incident, "message_id": f"synthetic-message-{index}",
            "thread_id": thread, "post_call_id": post, "cutoff": "2026-09-16T01:03:00.2451908Z",
            "response_path": f"{case_id}/response.txt", "context_path": f"{case_id}/context.txt",
            "context_available_at": "2026-09-16T00:49:00Z",
            "log_files": {"customEvents": f"{case_id}/customEvents.json"},
            "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
            "synthetic": True, "mapping_verified": True,
            "selection_reason": "Synthetic fixture explicitly modeling a first diagnostic response; not a real IcM.",
            "expected_rows": {"customEvents": len(rows)}, "documents": documents,
            "replay_path": f"{case_id}/replay.json",
            "task_instructions": "Use read-only source evidence to identify the service region.",
        })
    write_json(directory / "cases.json", {"schema_version": "1", "target_real_cases": 10, "cases": cases})
    write_json(directory / "evaluation.json", {
        "policy_version": "synthetic-demo-v1", "trust_rules": [
            {"id": "demo-kusto", "source_kind": "kusto", "origin_prefix": "demo.cluster/gateway",
             "maximum_score": 1, "instructions": "Synthetic fixture log source; not a production authority rule."},
            {"id": "demo-doc", "source_kind": "document", "origin_prefix": "https://docs.example.test/region",
             "maximum_score": 1, "instructions": "Synthetic fixture document source."},
        ],
    })
    return directory / "cases.json"
