# Testing Patterns

## 1) Test Stack and Commands

- pytest >=8,<9; standard assertions and `unittest.mock`.
- HTTPX mock transports for network behavior.
- Optional Playwright for actual incident selection, dialogs and browser safety.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q tests\test_executor.py tests\test_scoring.py
.\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe
$env:SCORING_SERVICE_BROWSER_CHANNEL = "msedge"
.\.venv\Scripts\python.exe -m pytest -q -m browser
```

The browser extra and an installed supported browser are prerequisites for the last command. No coverage command is configured.

## 2) Test Layout

Tests are separated by module in `tests`. `tests\conftest.py` generates local synthetic corpora using `create_demo()`. Temporary directories isolate files. Tests that need Git initialize temporary repositories; they do not commit or push the working project.

## 3) Test Scope Matrix

| Scope | Covered | Targets | Evidence |
|---|---|---|---|
| Unit | Yes | Scores, enum/range contracts, times, roster/import shapes | `test_scoring.py`, `test_imports.py`, `test_corpus.py` |
| Integration | Yes, synthetic/mocked | Gate short circuit, replay, temporal exclusions, claim bindings | `test_executor.py`, `test_bundle.py`, `test_judges.py` |
| Documents | Yes, snapshots/mock transport | Metadata, cache integrity, host/path controls | `test_documents.py` |
| Runtime audit | Yes | JSONL, ordering, credentials, stage events/failures | `test_runtime_log.py`, `test_runtime_integration.py` |
| Viewer/server | Yes | Escaping, private-file denial, contribution consistency | `test_report.py` |
| Browser | Yes, opt-in locally | Incident selection, dimension dialogs and keyboard behavior | `test_browser.py` |
| Copilot judge adapter | Yes, mocked; operator smoke is environment-dependent | SDK isolation, strict output validation and cache | `test_judges.py`; use an approved synthetic case for signed-in smoke testing |
| Real incident quality | Not established by synthetic suite | SRE-reviewed labels and complete evidence | `TASKS.md` |

## 4) Mocking and Isolation Strategy

Model replays contain explicit synthetic identities. Real replay requires request/model bindings. Transport injection avoids external network calls in normal tests. Tests verify failure paths rather than replacing absent evidence with passing defaults.

## 5) Coverage and Quality Signals

- [TODO] No coverage percentage or enforced threshold was found.
- Existing tests assert scores, preservation of zero/unavailable states, precise temporal boundaries, privacy guards and absence of future evidence.
- CI defines Windows/Linux and Python 3.11/3.12 jobs plus a browser job; a configured workflow is not proof that remote runs passed.
- [TODO] Hosted-CI outcomes and variability on the real incident corpus need separate observation.

## 6) Evidence

- `pyproject.toml`
- `.github\workflows\ci.yml`
- `tests\conftest.py`
- `tests\test_executor.py`
- `tests\test_runtime_integration.py`
- `tests\test_browser.py`
