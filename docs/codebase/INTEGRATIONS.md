# External Integrations

## 1) Integration Inventory

| System | Status/type | Purpose | Authentication | Evidence |
|---|---|---|---|---|
| Local incident/telemetry files | Implemented filesystem input | Frozen evidence | Local filesystem access | `imports.py`, `bundle.py` |
| OpenAI-compatible model APIs | Implemented optional live HTTP backend | Current live judgments | Explicit bearer or `api-key` env value, approved exact host | `judges.py`, `config.py` |
| Referenced document servers | Implemented restricted HTTP GET | Document snapshots and metadata | Optional configured bearer credential; allowed hosts | `documents.py` |
| Local model/document caches | Implemented filesystem | Integrity-checked replay/reuse | Local permissions | `judges.py`, `documents.py` |
| GitHub Copilot SDK/runtime | Implemented optional `copilot` backend | Copilot-managed judges without provider keys | Standard signed-in Copilot identity | `pyproject.toml`, `judges.py`; official SDK docs below |
| Live IcM / Application Insights API | No runtime collector implemented | Manual preparation only | Outside evaluator | `corpus.py`, `tools\export-queries.kql` |

## 2) Data Stores

| Store | Role | Access layer | Key risk | Evidence |
|---|---|---|---|---|
| `data` | Private corpus/roster/snapshots | Local import | Incomplete exports or unverifiable mappings | `imports.py`, `bundle.py` |
| `.cache` | Judge and document records | Hash-bound readers/writers | Reusing changed inputs or versions | `judges.py`, `documents.py` |
| `out` | JSON results, HTML and runtime journal | CLI/report/journal | Contains private evidence; not public artifacts | `cli.py`, `runtime_log.py`, `.gitignore` |

There is no application database, queue or message bus in the declared dependencies/source flow.

## 3) Secrets and Credentials Handling

- Direct HTTP mode reads configured environment variables. Copilot mode uses the SDK's signed-in-user path and does not read those endpoint credentials.
- Public configuration contains placeholders and approval flags, not active keys. Private config and data are ignored by Git.
- The Copilot SDK's documented signed-in-user path uses stored Copilot credentials; separate provider keys are not required for that path. Do not extract tokens from Copilot internals or call undocumented internal endpoints.
- Operators must verify account/model availability and the organization's allowance for this incident-data workload before setting the explicit approval flag.
- Being enabled for Copilot establishes product/model access, not a blanket declaration that every data classification or automation scenario is approved.
- Copilot identity/model access does not grant Azure tenant access or solve missing Application Insights/IcM exports.

## 4) Reliability and Failure Behavior

- Judge HTTP and Copilot SDK calls use configured timeouts, a bounded attempt budget, bounded schema repair and no model/provider fallback.
- A response reporting an unexpected model identity is rejected.
- Document fetching has bounded requests/redirects/size, preserves metadata and reports unsupported or unavailable content.
- No circuit breaker is implemented. Replay is an explicit mode, not an automatic success fallback.
- The Copilot adapter keeps three valid responses mandatory where required; an unavailable model is an error, not permission to substitute another family silently.

## 5) Observability for Integrations

- `executor.py` records judge-started/returned/failed events, input provenance, decisions and calculations.
- `RuntimeJournal` writes the separate private log. The static server does not expose logs or results JSON.
- Copilot usage/budget events and SDK/session identifiers are not yet added to the result contract; model ID, request hash, prompt hash and backend provenance are recorded.
- New live judgments are not deterministic; cached judgments plus deterministic scoring preserve reproducibility.

## 6) Evidence

- `src\scoring_service\judges.py`
- `src\scoring_service\documents.py`
- `src\scoring_service\config.py`
- `src\scoring_service\runtime_log.py`
- `config\evaluation.example.json`, `pyproject.toml`, `.gitignore`
- SDK/authentication: https://github.com/github/copilot-sdk/blob/main/docs/auth/authenticate.md
- Python SDK and model discovery: https://github.com/github/copilot-sdk/blob/main/python/README.md
- Organization model/feature policy: https://docs.github.com/en/copilot/how-tos/administer-copilot/manage-for-organization/manage-policies
- Usage/billing: https://github.com/github/copilot-sdk/blob/main/docs/features/usage-and-billing.md

SDK usage consumes the applicable Copilot allowance/billing; the three-judge design is not free/unlimited merely because there are no provider keys. SDK tools may be available by default: disable/deny judge tools and isolate sessions rather than copying an `approve_all` quick-start example.
