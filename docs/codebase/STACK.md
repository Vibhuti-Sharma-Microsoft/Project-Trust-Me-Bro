# Technology Stack

## 1) Runtime Summary

| Area | Value | Evidence |
|---|---|---|
| Primary language | Python; browser presentation uses JavaScript/CSS | `src\scoring_service`, `src\scoring_service\assets\report.js` |
| Runtime | Python >=3.11; CI selects 3.11 and 3.12 | `pyproject.toml`, `.github\workflows\ci.yml` |
| Package manager | pip, with development constraints | `requirements-dev.lock`, `README.md` |
| Build | Hatchling wheel; package `scoring_service` | `pyproject.toml` |
| Execution | Single-process batch CLI; separate loopback static server | `src\scoring_service\cli.py`, `report.py` |

## 2) Production Frameworks and Dependencies

| Dependency | Declared range | Role | Evidence |
|---|---|---|---|
| Pydantic | >=2.7,<3 | Strict input, configuration, judge and result contracts | `models.py`, `config.py`, `pyproject.toml` |
| HTTPX | >=0.27,<1 | Current direct judge API transport and permitted document GETs | `judges.py`, `documents.py` |
| Jinja2 | >=3.1,<4 | Escaped static report rendering | `report.py`, `templates\report.html.j2` |

Standard-library modules handle CLI parsing, hashes, files, concurrency and HTTP serving. No database/ORM or container runtime is declared. The GitHub Copilot SDK is **not currently a dependency or implemented judge backend** (`pyproject.toml`, `judges.py`).

## 3) Development Toolchain

| Tool | Purpose | Evidence |
|---|---|---|
| pytest >=8,<9 | Unit/integration tests | `pyproject.toml`, `tests` |
| Pyright >=1.1.380,<2 | Standard-mode type checking | `pyproject.toml` |
| Playwright >=1.50,<2, optional browser extra | Real browser selection/dialog checks | `tests\test_browser.py`, `pyproject.toml` |
| Git + publication guard | Repository and private-file checks | `tools\check_publish.py`, `.gitignore` |
| EditorConfig / Git attributes | UTF-8 and LF conventions | `.editorconfig`, `.gitattributes` |

## 4) Key Commands

From the repository root on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements-dev.lock -e ".[dev]"
.\.venv\Scripts\python.exe -m scoring_service --help
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m pip wheel --no-deps --wheel-dir dist .
```

## 5) Environment and Config

- Current HTTP live mode reads role endpoints/authentication from environment-variable names in `config\evaluation.example.json`; local settings belong in ignored `config\evaluation.local.json`.
- Replay uses local recorded outputs; unbound replay is only allowed for explicitly synthetic cases.
- Document fetching uses an explicit allowed-host policy and optional `document_auth_env`.
- There is no automatic `.env` loader in `config.py`/`judges.py`.
- [TODO] Pin and verify a compatible Copilot SDK/runtime release before implementing the requested no-BYOK backend. Do not assume the latest SDK documentation matches an arbitrary installed CLI version.

## 6) Evidence

- `pyproject.toml`
- `requirements-dev.lock`
- `.github\workflows\ci.yml`
- `src\scoring_service\config.py`
- `src\scoring_service\judges.py`

