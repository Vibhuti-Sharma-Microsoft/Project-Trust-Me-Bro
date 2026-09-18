# Scoring Service

A local, response-level evidence scorer. It reads frozen incident/log files, evaluates the initial todo plan, scores logical response-supporting steps, and writes a minimal incident scorecard plus separate detailed results and runtime logs. It never executes the SRE agent's logged tools or writes to IcM.

**Current scope:** four tri-valued dimensions and an initial-todo gate. This is an uncalibrated evidence index, not a probability of correctness. The bundled demo is **ten synthetic cases**, not ten collected real incidents.

**Start here:** [Source walkthrough](docs/codebase/ARCHITECTURE.md) | [Local setup](#local-setup) | [Synthetic demo](#run-the-complete-synthetic-replay) | [Real pilot preparation](#prepare-the-real-pilot-roster) | [Remaining tasks](TASKS.md) | [Two-person workflow](CONTRIBUTING.md)

## What is implemented

```text
Local case manifest + frozen incident/log snapshots
  -> validate response identity, initial todo, and pre-post evidence
  -> three-model todo gate
  -> GPT claim/step mapping
  -> three-model faithfulness + GPT source trust
  -> deterministic coverage, document freshness, weighted scoring
  -> single-incident HTML scorecard + results.json + scoring-service.log
```

Incident and telemetry reads are local during execution. Live judging and permitted document fetching are optional, explicit modes. The normal replay demo does not contact Azure, IcM, or a model provider.

The real pilot's incident list is selected, but collecting IDs is **not** the same as having a replayable corpus. Initial todo payloads, actual comment bodies, verified mappings, raw logs and model configuration remain separate inputs. See [TASKS.md](TASKS.md) for acceptance criteria and owners.

## Local setup

Requires Python 3.11+ and Git. Start in the standalone `scoring-service` repository, not the parent folder containing other repositories.

On a fresh checkout, create an isolated environment and install the tested dependency versions. The constraints file contains no machine-local editable paths.

```powershell
git clone <YOUR_APPROVED_PRIVATE_REPOSITORY_URL> scoring-service
cd scoring-service
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements-dev.lock -e ".[dev]"
```

On macOS/Linux, the virtual-environment interpreter is `.venv/bin/python`; use it in place of the Windows interpreter in the examples. Activation is optional. An activated environment also lets you use `python -m scoring_service` directly.

No `.env`, Azure login, model key, or real incident export is needed to run unit tests and the synthetic demo.

On Windows, prefer a short checkout path such as `C:\repos\scoring-service`. Very deeply nested checkouts can exceed Windows path limits while installing Pyright's bundled type stubs. If installation reports a missing path deep inside `pyright`, retry from a shorter checkout path; no machine-wide setting change is required.

## Run the complete synthetic replay

No credentials or network access are needed for replay:

```powershell
.\.venv\Scripts\python.exe -m scoring_service demo --directory data\demo
.\.venv\Scripts\python.exe -m scoring_service validate --manifest data\demo\cases.json
.\.venv\Scripts\python.exe -m scoring_service evaluate `
    --manifest data\demo\cases.json `
    --config data\demo\evaluation.json `
    --judge-mode replay --run-id demo
.\.venv\Scripts\python.exe -m scoring_service serve --directory out\demo --port 8765
```

Open `http://127.0.0.1:8765`. You can also open `out\demo\index.html` directly.

Select one incident from the dropdown. The scorecard shows its total and four clickable dimension contributions. Clicking a dimension opens a brief definition, calculation and case-specific explanation. Dimension tiles show their weighted **points toward the total**, not newly averaged 0/0.5/1 judgments; individual step judgments and scoring rules are unchanged.

The HTML deliberately omits raw telemetry, full prompts, lengthy evidence dumps and all-case diagnostic tables. Gate failures and unavailable results remain distinct. Detailed evaluator input, decisions, citations and arithmetic are written separately to `out\<run-id>\scoring-service.log`.

The demo intentionally contains gate failure, missing todo, and judge-error cases. Consequently, its batch `evaluate` command returns exit code **1**, while still writing the complete report. Successful cases and gate-failed score-zero cases are not execution errors.

## Run the local real-incident pilot

The local workspace's default `evaluate` command targets `data\real-pilot\cases.json`, uses `config\evaluation.local.json`, and invokes the Copilot SDK. It does not evaluate the synthetic demo unless a demo manifest and replay mode are explicitly supplied.

```powershell
.\.venv\Scripts\python.exe -m scoring_service validate `
    --manifest data\real-pilot\cases.json
.\.venv\Scripts\python.exe -m scoring_service evaluate `
    --run-id real-pilot
```

The current corpus contains six validated real incidents. Four requested incidents are explicitly excluded in `data\real-pilot\excluded-incidents.json` because the available exports do not contain a usable target posting/thread. The configured third panel slot uses an explicitly selected Grok model because the signed-in Copilot model catalog does not currently expose Gemini; no model is selected by fallback.

| Case | Expected result |
|---|---|
| Supported | 100 |
| Partial/model disagreement | 65 |
| Contradicted | 30, with the contradiction exposed; no unapproved global cap is added |
| Incorrect initial todo | 0; all downstream evaluation is skipped |
| Missing todo | Unscorable, not zero |
| Stale document | 90; document freshness is 0 |
| Missing document update date | 90; freshness is 0 with an explicit reason |
| Unreviewed source | 80; source trust is 0 |
| Missing required step | 0 contribution, retained in denominator |
| Missing judge response | Judge error; no score and no single-model fallback |

Demo generation refuses to overwrite a nonempty directory, and evaluation refuses to overwrite an existing run directory. Choose a new directory/run ID for another run; no commands delete your data.

To run just one case, add `--case synthetic-supported`. Omit `--run-id` to generate a new output directory automatically. To regenerate HTML without calling a judge:

```powershell
.\.venv\Scripts\python.exe -m scoring_service render --results out\demo\results.json
```

If port 8765 is in use, choose another `--port`. Stop your report server with Ctrl+C.

## Prepare the real pilot roster

Real incident IDs, mappings, logs, documents, caches and generated reports stay outside Git. In the original development workspace, the selected ten-incident roster is saved at **`data\incident-roster.local.json`**. Share it through an approved internal channel, not through a public issue or committed dataset.

On another checkout, create that private file from `examples\incident-roster.example.json` and populate `incident_ids` with the agreed decimal-string IDs. The empty public example is intentionally not runnable.

```powershell
New-Item -ItemType Directory -Force data | Out-Null
if (-not (Test-Path data\incident-roster.local.json)) {
    Copy-Item examples\incident-roster.example.json data\incident-roster.local.json
}
# Populate the private roster before running this command.
.\.venv\Scripts\python.exe -m scoring_service prepare-corpus `
    --incidents-file data\incident-roster.local.json `
    --directory data\real-pilot
```

This command makes no network calls. It creates:

- `collection-plan.json`: one pending collection entry per incident, preserving any supplied verified response mapping.
- `queries\01-discover-threads.kql`: identifies candidate logged threads; reports incidents not found in the chosen scope/window.
- `queries\02-posting-candidates.kql`: lists posting attempts for choosing the first diagnostic response.
- An empty destination folder for each incident's actual exports.

**`collection-plan.json` is a checklist, not an evaluation manifest or test result.** Do not pass it to `evaluate`. After collecting and verifying the artifacts, create `cases.json` using the contract below. Older incidents may be outside telemetry retention or in a different Application Insights resource; record that limitation instead of inventing missing evidence.

The canonical IcM message ID comes from the discussion record, not from the thread ID. A `PostDiscussionEntry` acknowledgment may not include it. Preserve the independently verified message-to-CallId association; never choose an arbitrary nearest timestamp.

## Scoring contract

1. **Initial todo gate:** GPT, Claude and Gemini independently return PASS, FAIL or INSUFFICIENT_EVIDENCE. Two PASS votes proceed; two FAIL votes stop with score 0; other combinations stop unscorable. A missing/failed/malformed judge response is an error, not a vote.
2. **Default GPT:** extracts exact material response quotes and associates them with logical todo steps and producing calls.
3. **Faithfulness:** GPT, Claude and Gemini judge claims against evidence. The dimension uses the median vote.
4. **Coverage:** GPT provides supported/partial/unsupported/contradicted claim classifications. Code assigns 1 if all claims are fully supported, 0.5 if some support exists, and 0 if none does.
5. **Source trust:** GPT applies reviewed source rules. Code rejects unknown rules, invented citations and scores exceeding the authority permitted for the cited evidence.
6. **Freshness:** documents only. Code scores verified update age at the response cutoff: through 365 days = 1, through 1,095 days = 0.5, older/unknown/future/unverified = 0. For several referenced documents, the minimum applies.

**No referenced documents gives freshness 1 by your explicit neutral convention.** This is not verified freshness. A document lookup that failed or has no descriptor/date is not a no-document step.

Each scored dimension is exactly `0`, `0.5`, or `1`.

```text
step score = 35*faithfulness + 35*coverage + 20*source trust + 10*document freshness
response score = sum(included step contributions) / included step count
```

Logical steps, not individual retries, receive equal aggregation weight. Missing required work and unassigned material claims retain zero-contribution entries. Housekeeping and evidence-backed conditional exclusions stay in the audit view but not the denominator. A failed todo gate overrides everything with 0.

Log recency, resource/query scope and time-window adequacy are part of faithfulness. Neither a zero-row result nor a successful transport response proves service health.

## Import real cases

`data\cases.json` is a local manifest. Paths are relative to its directory and must remain inside it. Use one selected diagnostic response per incident; do not substitute a whole thread or progress message.

```json
{
  "schema_version": "1",
  "target_real_cases": 10,
  "cases": [{
    "id": "incident-EXAMPLE-first",
    "incident_id": "REPLACE_WITH_INCIDENT_ID",
    "icm_instance": "portal.microsofticm.com",
    "message_id": "REPLACE_WITH_VERIFIED_MESSAGE_ID",
    "thread_id": "REPLACE_WITH_VERIFIED_THREAD_ID",
    "post_call_id": "REPLACE_WITH_VERIFIED_POST_CALL_ID",
    "cutoff": "2026-01-01T01:03:00.2451908Z",
    "response_path": "incident-EXAMPLE/response.html",
    "response_sha256": "REPLACE_WITH_SHA256_OF_RESPONSE_FILE_BYTES",
    "context_path": "incident-EXAMPLE/context.txt",
    "context_available_at": "2026-01-01T00:49:48.413Z",
    "task_instructions": "The actual task/workflow requirements available for the initial plan",
    "log_files": {
      "customEvents": "incident-EXAMPLE/customEvents.json",
      "dependencies": "incident-EXAMPLE/dependencies.json",
      "genAIContent": "incident-EXAMPLE/genAIContent.json"
    },
    "mapping_verified": false,
    "selection_reason": "Replace with actual mapping evidence; set mapping_verified only after verification",
    "synthetic": false,
    "documents": []
  }]
}
```

This example contains placeholders and illustrative timestamps; it is **not** a completed real-data fixture. Replace them with actual comment snapshots, timestamps, hashes, raw logs, full initial todo arguments, and context with verified as-of provenance before running it.

Supported telemetry inputs:

- JSON arrays of raw records.
- JSONL, one raw record per line.
- Application Insights CSV with quoted JSON `customDimensions` cells, including UTF-8 BOM.
- A Query API JSON envelope containing one result table.

Require an ISO/RFC3339 `timestamp` with a timezone. Export timestamps in ISO form instead of locale-dependent formatted dates. Preserve native column names and JSON values. `allTableSchema.csv` and `sre-agent-telemetry-data.csv` are aggregate inventories and are deliberately rejected as raw telemetry.

Run `tools\discover-incident.kql` first. It scans only `customEvents` for one incident and returns the candidate thread plus first/last snapshot timestamps. Set the Application Insights portal time picker to include the same UTC range; the portal applies its time range in addition to KQL.

Then run `tools\export-queries.kql` with the discovered/verified thread and a narrow UTC window around the incident—normally one hour before `FirstSeen` through one hour after the selected posting cutoff. The export query no longer performs a 180-day multi-table discovery scan. It exports all six supported tables in one native-column result using `SourceTable`; there is no table selector to keep editing. The evaluator itself has no live incident/Kusto client. The query avoids a `pack_all()` JSON envelope because the envelope creates oversized cells that are easy to truncate. Export CSV from the portal download action, not by copying the result grid.

If a result approaches the portal row/download limit, use non-overlapping `[SliceStart, SliceEnd)` windows and download each slice. Split and merge all slices for that incident with:

```powershell
.\.venv\Scripts\python.exe -m scoring_service split-export `
    --incident-id 868627855 `
    --input downloads\868627855.2026-03.csv `
    --input downloads\868627855.2026-04.csv `
    --directory data\real-pilot\incidents\868627855
```

The command verifies the incident ID and table names, refuses to overwrite existing CSVs, and produces `customEvents.csv`, `dependencies.csv`, `genAIContent.csv`, `traces.csv`, `requests.csv`, and `exceptions.csv` for tables that had rows. Run the KQL count variant for every slice and verify that the command's table counts equal the sum of `ExpectedRows`.

Initial todo parser contracts currently support `ManageTodoList` inputs containing a `todos`, `todoList` or `todo_list` array with `id`, `title` (or `description`) and status. The earliest unavailable/corrupt or already-completed initial plan is not replaced with a later convenient one.

## Source identity and temporal limits

- IcM `messageId` is external to the logs. The manifest records the verified comment-to-posting-CallId association.
- Validate the selected response hash, target incident in posting arguments, and exact cutoff.
- Preserve sub-microsecond timestamp precision when rejecting evidence completed at/after the cutoff.
- Pair and deduplicate actual records; do not treat ToolEnd as a business-success flag.
- Additional tables and GenAI content are imported as diagnostic context. Copies of a result in different tables are not independent source evidence.
- An explicit carry-forward call-ID list is required for evidence from a different invocation in the same thread.
- Required redacted/truncated evidence is unavailable; intact fragments may still be quoted with their limitations. A size-boundary/ellipsis indicator alone does not prove truncation.
- Context without a verified availability timestamp prevents the initial-plan gate from being scored.

## Documents

Add document descriptors to the case manifest:

```json
{
  "id": "tsg-1",
  "url": "https://approved-doc-host.example/path",
  "step_id": "step-1",
  "version": "PINNED_VERSION",
  "call_ids": ["THE_RECORDED_DOCUMENT_TOOL_CALL_ID"],
  "snapshot_path": "documents/tsg-1.txt",
  "snapshot_sha256": "SHA256_OF_EXACT_SNAPSHOT_CONTENT",
  "last_updated": "2025-11-01T00:00:00Z",
  "historical_version_verified": true
}
```

A local snapshot works in replay. Live mode can fetch an allowlisted HTTPS URL on port 443 and cache the exact version. Authentication, redirect, unsupported encoding/compression, size and metadata failures are explicit evidence statuses. Credentials are never forwarded to a different host.

Historical verification requires a declared association backed by matching content/version evidence; retrieval today is not proof that a document existed in that form when the response was posted. Missing or unverified dates stay unavailable. The cache is integrity checked but is not a cryptographically signed evidence store.

Associate every referenced document call with a descriptor, using the exact requested URL or explicit `call_ids`. Describing document A does not satisfy a separate request for document B. Future-version document content is withheld from semantic judges, not merely assigned a lower freshness value.

## Copilot-managed judges

The `copilot` judge mode uses the official GitHub Copilot SDK and the signed-in user's Copilot access. It does not require personally supplied model endpoints or API keys. The existing evaluator still validates evidence references, enforces complete panels, calculates scores deterministically and writes the audit record.

Runtime flow:

```text
Terminal or Copilot -> Python executor -> Copilot SDK judge adapter
  -> signed-in Copilot runtime -> explicitly selected available models
  -> existing validation, deterministic scoring, audit log and report
```

Install dependencies, authenticate the Copilot CLI/runtime for the current user, and download the pinned runtime once (the SDK can also download it on first use):

```powershell
.\.venv\Scripts\python.exe -m copilot download-runtime
Copy-Item config\evaluation.copilot.example.json config\evaluation.local.json
```

Edit `config\evaluation.local.json` with the exact Copilot model IDs available to the signed-in account, set `approved_for_incident_data` to `true` only after the applicable data-policy review, and configure reviewed `trust_rules`. Endpoint and token environment variables are not used in this mode.

```powershell
.\.venv\Scripts\python.exe -m scoring_service evaluate `
    --manifest data\cases.json `
    --config config\evaluation.local.json `
    --judge-mode copilot
```

Each request creates an isolated SDK `empty` session with no available tools, memory disabled, session-store access disabled, no inherited repository instructions/environment context, and one explicitly selected model. There is no model fallback. Validated responses use the existing hash-bound cache.

The exact available models, organization policies, permitted incident-data use and usage allowance must still be checked. No Copilot token extraction or undocumented endpoint calls are used.

See [Architecture](docs/codebase/ARCHITECTURE.md) and [Integrations](docs/codebase/INTEGRATIONS.md) for implementation and operational constraints.

## Existing optional direct-HTTP judge backend

Copy `config\evaluation.example.json` to ignored `config\evaluation.local.json` and configure each role explicitly. Supported transports are **OpenAI-compatible Chat Completions and Responses**. This includes Claude/Gemini only when your approved enterprise endpoint exposes one of those protocols. Native vendor APIs are not silently substituted.

These endpoint/key instructions apply only to `--judge-mode live`; they are not required for `--judge-mode copilot`.

For each role configure:

- The exact model/deployment and exact allowed endpoint host.
- `endpoint_env`: environment variable containing the full request URL.
- `auth_env`: environment variable containing its credential; use `Authorization` for bearer auth or `api-key` for that header.
- Correct `protocol`, and `approved_for_incident_data: true` only after approval.

Do not put tokens in configuration files, command arguments, reports, or source control. The example is intentionally non-runnable for live requests. Configure reviewed trust rules and permitted document hosts separately.

```powershell
.\.venv\Scripts\python.exe -m scoring_service evaluate `
    --manifest data\cases.json `
    --config config\evaluation.local.json `
    --judge-mode live
```

All three models are required for gate/faithfulness panels. Errors are explicit; the service does not fall back to GPT or retry for a more favorable judgment. Model-visible logs and documents are untrusted data, not instructions or executable tools.

Packaged prompts are in `src\scoring_service\prompts`. Calls are cached by model/endpoint protocol, payload, prompt/schema, policy and inference settings. Successful cached responses are schema validated on reuse.

For real replay, each entry is bound to the exact request hash and model. Keys are `todo_gate:gpt`, `claims:gpt`, `step:step-1:gpt`, etc., with `{ "model": "...", "request_sha256": "...", "output": {...} }`. Unbound responses are permitted **only** for explicitly synthetic cases. `JudgeService.request_key(...)` supplies the binding. Persisted `CaseResult.judges` records contain those hashes and outputs for constructing a real replay file.

## Results, errors and verification

Each new evaluation produces:

| File | Purpose |
|---|---|
| `index.html`, `report.css`, `report.js` | Minimal single-incident scorecard with clickable dimension explanations |
| `results.json` | Complete structured result: evidence references, gate votes, claims, step judgments and versions |
| `scoring-service.log` | Detailed chronological JSON Lines runtime audit, kept separate from the scorecard |

The runtime log records import/cutoff decisions, initial todo selection, each judge's input and returned explanation, gate decisions, claim/step mappings, document availability, exclusions, dimension calculations, weighted contributions and failures. It includes supplied judge rationales, not private model scratchpads. Explicit credential fields and conventional credential strings are masked; private incident content can still be present, so keep the log in ignored `out\` and share only through approved channels.

Every event includes a sequence, UTC recording time, event name and case ID. To inspect one case locally:

```powershell
Get-Content out\demo\scoring-service.log |
    ForEach-Object { $_ | ConvertFrom-Json } |
    Where-Object case_id -eq "synthetic-supported" |
    ConvertTo-Json -Depth 100
```

`render` rebuilds the compact UI without live calls and does not invent a runtime log for an older result. Null scores remain null; a batch never hides unscorable/error cases or relabels them as score 0.

Exit codes: 0 = command completed without unscorable/error cases, 1 = evaluated batch contains unavailable/error cases, 2 = invalid configuration/input/command state, 130 = interrupted.

The localhost server exposes only the report HTML/CSS/JavaScript; it does not serve the runtime log, incident JSON, corpus, caches, credentials, parent directories or arbitrary files. It refuses non-loopback hosts.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m pip wheel --no-deps --wheel-dir dist .
```

For browser interaction checks on Windows with Microsoft Edge installed:

```powershell
.\.venv\Scripts\python.exe -m pip install -c requirements-dev.lock -e ".[browser]"
$env:SCORING_SERVICE_BROWSER_CHANNEL = "msedge"
.\.venv\Scripts\python.exe -m pytest -q -m browser
```

For Chromium, install the browser using `python -m playwright install chromium` and select channel `chromium`. Browser tests are optional locally; CI runs them on its Linux/Python 3.11 job.

Real-data acceptance still requires the curated 10-incident corpus, approved endpoint configurations and source-policy review. Replay validates implementation behavior, not the accuracy of live LLM judges.

## Repository layout and collaboration

| Area | Purpose |
|---|---|
| `src\scoring_service\imports.py`, `bundle.py`, `corpus.py` | Local collection preparation, raw imports, response selection and evidence normalization |
| `src\scoring_service\documents.py` | Permitted document fetching, pinned snapshots and integrity-checked cache |
| `src\scoring_service\judges.py`, `prompts` | Model protocols, strict judgment contracts and replay/cache |
| `src\scoring_service\scoring.py`, `executor.py` | Gate and dimension aggregation, claim/step coordination |
| `src\scoring_service\runtime_log.py` | Detailed, credential-sanitized runtime audit separate from the UI |
| `src\scoring_service\report.py`, `templates`, `assets` | Minimal selectable scorecard and loopback-only report serving |
| `config`, `examples`, `tools` | Safe templates; do not replace them with private credentials/data |
| `tests` | Synthetic fixtures and mocked network tests |
| `docs\codebase` | Source-backed stack, structure, architecture, conventions, integrations, testing and concerns guides |
| `data`, `.cache`, `out` | Private local files, all Git-ignored |

Follow [CONTRIBUTING.md](CONTRIBUTING.md) for branch ownership, cross-review and a safe first push. [TASKS.md](TASKS.md) divides the remaining work into data/evidence and judges/evaluation tracks.

GitHub Actions is configured to run publish-candidate checks, tests, type checking and package builds on Windows and Linux with Python 3.11/3.12 after the repository is pushed, plus browser interactions on Linux/Python 3.11. It uses synthetic/mocked inputs only and does not require deployment secrets.
