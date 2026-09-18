# Coding Conventions

## 1) Naming Rules

| Item | Observed rule | Example | Evidence |
|---|---|---|---|
| Files/functions | snake_case | `runtime_log.py`, `build_bundle()` | `src\scoring_service` |
| Types | PascalCase | `CaseSpec`, `JudgeService` | `models.py`, `judges.py` |
| Internal helpers | Leading underscore | `_panel`, `_live_endpoint` | `executor.py`, `judges.py` |
| Constants | UPPER_CASE | `ROLES`, `TABLES` | `scoring.py`, `imports.py` |
| Env names | Explicit names selected by configuration | `endpoint_env`, `auth_env` | `config.py`, configuration example |

## 2) Formatting and Linting

- `.editorconfig` specifies UTF-8, LF, spaces and a final newline; JSON/YAML use two-space indentation.
- `.gitattributes` normalizes text to LF.
- Pyright standard mode is configured; it is a type checker, not a general formatter.
- No Black/Ruff/general lint configuration is declared in `pyproject.toml`.
- Commands: `python -m pyright --pythonpath <venv-python>` and `git diff --check`.

## 3) Import and Module Conventions

- Application modules use relative imports; tests use the public package namespace.
- Standard-library, dependency and application imports are generally grouped; no import-order enforcement tool is configured.
- Entry selection is explicit in `__main__.py` and `pyproject.toml`; there is no barrel-export layer.
- Corpus paths are resolved through `imports.safe_path()`, not by accepting arbitrary absolute paths from data.

## 4) Error and Logging Conventions

- Import failures become explicit `IMPORT_ERROR` case results.
- Judge failures remain `JUDGE_ERROR`; they are not incorrect-plan votes or scored zero.
- Gate rejection, unavailable evidence, non-factual responses and valid scored-zero results have different statuses.
- Runtime log failures raise `RuntimeLogError`, rather than silently continuing without an audit.
- Logs are ordered private JSON Lines with `sequence`, `recorded_at`, `event`, `case_id`, `details`.
- Credential patterns are sanitized. This is explicitly not a general PII detector; privacy controls still matter.

## 5) Testing Conventions

- Tests live in `tests\test_*.py`; `conftest.py` generates synthetic local corpora.
- HTTP transports, model outputs and filesystem conditions are mocked or locally injected.
- Browser checks are opt-in locally using the browser extra and `SCORING_SERVICE_BROWSER_CHANNEL`.
- [TODO] No measured coverage percentage or configured minimum coverage threshold was found.

## 6) Evidence

- `.editorconfig`, `.gitattributes`, `pyproject.toml`
- `src\scoring_service\imports.py`
- `src\scoring_service\executor.py`
- `src\scoring_service\runtime_log.py`
- `tests\conftest.py`

