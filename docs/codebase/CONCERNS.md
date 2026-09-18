# Codebase Concerns and Intent Gaps

## 1) Top Risks (Prioritized)

| Priority | Concern | Evidence | Impact | Next action |
|---|---|---|---|---|
| Medium | Copilot model/data-policy availability is environment-specific | `copilot` mode requires explicit model IDs and approval flags | A configured model can still be unavailable or disallowed for incident data | Verify account catalog and organizational policy before a real run |
| High | Selected incident IDs are not a complete replayable corpus | Preparation emits AWAITING_EXPORTS; bundle requires actual artifacts | Real cases cannot honestly receive scores from missing data | Collect and verify local snapshots/exports |
| High | Temporal/source identity must remain intact during backend changes | Bundle/reference/document guards | A later correction or unrelated context could inflate an earlier response | Preserve guards and regression tests |
| Medium | Model judgment variability is not calibrated correctness | Three votes, synthetic fixtures, no reviewed real baseline | A numeric score is not a probability | Label as evidence index; review real examples |

## 2) Technical Debt

| Item | Where/evidence | Risk | Suggested change |
|---|---|---|---|
| Copilot usage metadata is limited | `JudgeRecord` captures model/backend/request hashes, not SDK usage events | Cost/allowance analysis requires external runtime telemetry | Add reviewed usage fields without storing sensitive content |
| Limited recognized todo argument shapes | `_todo_from_call()` in `bundle.py` | Other logged shapes become unavailable rather than scored | Add explicit adapters with real fixtures |
| Candidate Kusto names are not full lineage resolution | Export/source preparation and README limitations | Incorrect authority assumptions for aliases/functions | Retain unresolved provenance or add a verified resolver |
| Large judge module | `judges.py`, 706 lines at inspection | Auth/replay/cache/validation changes can couple unexpectedly | Introduce the backend seam with contract tests, not a broad rewrite |

## 3) Security and Data-Boundary Concerns

These are design risks/controls, not a claim of an exploit audit.

| Risk | Category | Current evidence/mitigation | Remaining gap |
|---|---|---|---|
| Untrusted evidence influencing an agent runtime | Prompt injection | HTTP has no tools; Copilot uses isolated `empty` sessions with `available_tools=[]`, memory and session store disabled | Keep SDK isolation options covered by adapter tests during upgrades |
| Private evidence leaking through sharing | Data exposure | Ignored data/cache/output, credential redaction, loopback asset allowlist | Manual data-classification and sharing controls remain necessary |
| Incorrect assumption that model enablement approves every dataset | Governance | External policy controls access; code currently has explicit approval configuration | [TODO] Confirm permitted Copilot use for this incident-data class |
| Backend change bypassing temporal/reference checks | Integrity | Existing bundle/scorer guards and tests | Preserve checks after SDK output parsing |

## 4) Performance and Scaling Concerns

| Concern | Evidence | Current measured symptom | Risk/action |
|---|---|---|---|
| Cases execute sequentially | `cli.main()` list comprehension | [TODO] No real-corpus benchmark | Profile before parallelizing cases |
| Large evidence repeated in model payloads/audit | `executor.py`, request budget in `judges.py` | [TODO] Real payload distribution unknown | Keep limits and explicit failures; preserve IDs if batching/chunking |
| Three-model panels consume multiple requests | `_panel()` | No Copilot backend usage measured | Track actual Copilot usage/budget and cache validated results |

## 5) Fragile / High-Churn Areas

The available Git history has one initial commit (`6703117`), so it does not establish a meaningful high-churn ranking. Historical paths reflect the old package name; the rename and current UI/logging changes are in the worktree.

The judge module, temporal normalization and result/scoring contracts deserve focused regression coverage because of their responsibilities, not because an unobserved churn trend was inferred. The scan's apparent TODO matches in `bundle.py` are status strings containing `TODO`, not TODO comments.

## 6) Evidence

- `src\scoring_service\cli.py`
- `src\scoring_service\judges.py`
- `src\scoring_service\bundle.py`
- `src\scoring_service\executor.py`
- `src\scoring_service\scoring.py`
- `src\scoring_service\runtime_log.py`
- `TASKS.md`, `.gitignore`
- Read-only scan: source metrics, CI detection, TODO matches and single-commit history; private data/local config excluded.
- Official SDK default-tool and authentication guidance: https://github.com/github/copilot-sdk
