# Codebase Structure

## 1) Top-Level Map

| Path | Purpose | Evidence |
|---|---|---|
| `src\scoring_service` | Application modules | `pyproject.toml` package declaration |
| `config` | Public safe configuration example; private overrides are ignored | `config\evaluation.example.json`, `.gitignore` |
| `examples` | Empty incident-roster contract, not an executable real corpus | `examples\incident-roster.example.json` |
| `tools` | Read-only export template and publication checks | `tools\export-queries.kql`, `tools\check_publish.py` |
| `tests` | Synthetic and mocked fixtures, integration and browser checks | `tests\conftest.py`, `tests\test_browser.py` |
| `.github` | CI, PR and task templates | `.github\workflows\ci.yml` |
| `docs\codebase` | This source-backed walkthrough | Seven Markdown documents in this directory |
| `data`, `.cache`, `out`, `.venv` | Private/generated local state, not source conventions | `.gitignore`, `README.md` |

## 2) Entry Points

- `python -m scoring_service` loads `src\scoring_service\__main__.py`, which invokes `cli.main()`.
- Installed console command `scoring-service` points to the same function through `[project.scripts]` in `pyproject.toml`.
- Subcommands: `demo`, `prepare-corpus`, `validate`, `evaluate`, `render`, `serve`.
- `serve` is a static viewer; incident selection does not invoke judges or rerun scoring.
- No worker, scheduler, application API server, database daemon or MCP server is implemented.

## 3) Module Boundaries

| Module | Owns | Must not be mistaken for |
|---|---|---|
| `corpus.py`, `imports.py`, `bundle.py` | Preparation checklist, local import and response-scoped evidence | A live incident/telemetry collector |
| `models.py`, `config.py` | Data contracts and policy/configuration | Model-generated facts |
| `executor.py` | Stage order, short-circuiting, calls to judges and scorer | A tool-executing SRE agent |
| `judges.py`, `prompts` | Semantic judgment transport/validation/cache | Deterministic score arithmetic |
| `documents.py` | Controlled document snapshots/GET/cache | Arbitrary agent browsing |
| `scoring.py` | Votes, tri-valued coverage/freshness, trust ceiling and aggregation | A model self-rating |
| `runtime_log.py` | Private stage-level JSONL audit | A public web endpoint |
| `report.py`, templates/assets | Minimal HTML, interactions and restricted file serving | An evaluator backend |

## 4) Naming and Organization Rules

- Distribution/folder/console name: `scoring-service`; Python import namespace: `scoring_service`.
- Python modules and functions use snake_case; models/classes use PascalCase.
- Modules use explicit relative imports; tests import `scoring_service`.
- Templates and static assets are packaged beneath the Python package.
- Historic Git paths may show the previous package name; current source is the renamed worktree.

## 5) Evidence

- `pyproject.toml`
- `src\scoring_service\__main__.py`
- `src\scoring_service\cli.py`
- `src\scoring_service\assets\report.js`
- `.gitignore`

