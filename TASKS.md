# Remaining work and ownership

## Implemented

- Standalone Python CLI, strict data contracts and safe local file handling.
- CSV/JSON/JSONL raw telemetry imports, response/hash mapping checks and precise evidence cutoffs.
- Three-model todo gate and faithfulness; default-GPT claim/source-trust work.
- Tri-valued dimensions, document-only freshness and 35/35/20/10 normalized scoring.
- Approved endpoint transports, replay/cache and explicit model errors.
- Read-only document snapshots/fetch/cache with historical-version checks.
- JSON results, a minimal single-incident scorecard with clickable dimension summaries, and loopback-only serving.
- Detailed per-run evaluator analysis in a separate private `scoring-service.log`.
- Synthetic regression suite, ten-case replay demonstration, package build and CI.
- Real-incident roster preparation command. The selected private roster has ten incidents; its collection entries are still awaiting exports.

**Not yet demonstrated:** scoring the ten real cases with approved live judges and reviewed production source policies. Incident IDs alone do not make a completed test corpus. The no-BYOK Copilot SDK judge route is implemented, but a real baseline still requires approved models, policies and complete evidence.

## Backlog

| ID | Task | Owner | Dependency / status | Acceptance criteria |
|---|---|---|---|---|
| DATA-01 | Collect raw artifacts for all ten selected incidents | A | Roster selected; exports/access/retention needed | Actual first diagnostic body, task context, initial todo, raw pre-post calls and expected row counts for every case; unavailable data explicitly recorded |
| DATA-02 | Verify comment-to-posting-call mappings | A | DATA-01 | One selected response per real incident; message ID, CallId, trace, cutoff and content hashes match; ambiguous candidates rejected |
| DATA-03 | Build the executable real `cases.json` | A | DATA-01/02 | `validate` passes structural checks; missing todo/context/version warnings reviewed; collection checklist is not passed as an evaluation manifest |
| DATA-04 | Resolve document and source provenance | A | DATA-01 | Each referenced document maps to a call/URI/version; metadata or absence is explicit; KQL aliases/functions are not mislabeled as resolved physical sources |
| EVAL-01 | Integrate Copilot-managed GPT, Claude and Gemini judging | B | Implemented; Copilot sign-in/catalog/policy verification remains operational | No personally supplied provider keys required for the Copilot route; explicit available models, denied judge tools, schema/reference validation, recorded provenance and no silent fallback |
| EVAL-02 | Review production source-trust instructions | B, review A | DATA-04 and SRE review | Actual source origins have reviewed authority rules; unknown source handling remains conservative |
| EVAL-03 | Review gate/faithfulness/coverage rubrics with real examples | B, review A | DATA-03 | Small labeled set covers correct, partial, contradicted and insufficient cases; reasons cite real evidence |
| EVAL-04 | Execute and preserve a real-judge baseline | B | DATA-03, EVAL-01/02/03 | Ten explicit case outcomes, all votes/provenance saved, no silent skip/fallback, and bound replays reproduce arithmetic |
| UI-01 | Refine concise real-data dimension explanations | B | EVAL-04 | Incident selection and dimension summaries explain the real scores without moving raw diagnostic dumps back into the UI; the runtime log retains the full evidence/judgment trail |
| VERIFY-01 | Diagnose corpus-specific parser gaps | A | DATA-01/04 | Unsupported todo/document/export variants get fixtures and explicit adapters, not heuristic success defaults |
| VERIFY-02 | Jointly review false-high cases and missing-data behavior | A + B | EVAL-04 | Contradictions, duplicate evidence, delayed data and missing metadata do not receive unexplained high scores |
| RELEASE-01 | Push code to an approved GitHub repository | A or B | Destination/privacy approval | Clean reviewed commit, real data excluded, branch protection and CI enabled |
| RELEASE-02 | Final teammate/demo handoff | A + B | EVAL-04, VERIFY-02, RELEASE-01 | Fresh checkout runs synthetic demo; authorized local data/config can reproduce the real baseline; limitations documented |

## Parallel work sequence

1. A starts DATA-01/02; B starts EVAL-01 and verifies the chosen Copilot integration with synthetic inputs before passing private incident evidence.
2. Agree on any parser/result contract changes in a small jointly reviewed PR. A supplies one verified real bundle before expanding the remaining corpus.
3. A completes DATA-03/04 and parser adapters; B refines EVAL-02/03 and UI using contract fixtures.
4. B records the real baseline; both review disagreements and false-high examples.
5. Integrate only reviewed changes with green CI. Keep data-access and endpoint-approval blockers on the board rather than treating them as coding completion.

## Known limitations to preserve

- The IcM message ID is obtained externally; it is not a native Application Insights join key.
- A source request is not proof of successful retrieval or consumption.
- RegEx Kusto table/view candidates are not a complete lineage resolver.
- Some original values may be redacted, previewed or outside retention; do not reconstruct missing evidence from later summaries.
- OpenAI-compatible Chat Completions/Responses are supported. Native vendor APIs require explicit approved adapters if the selected gateway does not expose those protocols.
- `--judge-mode live` is direct HTTP. `--judge-mode copilot` uses the SDK signed-in-user path and avoids separate provider keys; applicable Copilot model/data policies still apply.
- Freshness covers documents only, with the agreed neutral 1 for genuinely no-document steps. It is not a separate numeric log-freshness score.
- No live incident discovery, Application Insights ingestion, IcM writing, deployment or calibrated correctness probability is implemented or implied.
