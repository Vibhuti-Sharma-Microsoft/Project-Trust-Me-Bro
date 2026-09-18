# Architecture and Source Walkthrough

## 1) Architectural Style

A modular, local batch application with a separate static viewer. Functions and typed models separate evidence preparation, orchestration, semantic judgment, deterministic scoring and presentation. It is not an agent framework or a deployed microservice.

Constraints visible in code: local incident/log inputs; one verified posted response per case; pre-post temporal eligibility; no incident writes or execution of logged commands; explicit unavailable/error outcomes.

## 2) System Flow

```text
__main__.py / installed console command
  -> cli.main()
  -> load_manifest() + load_config() + RuntimeJournal
  -> executor.evaluate_case()
      -> bundle.build_bundle()
      -> three-model todo gate
      -> GPT claim/step binding
      -> permitted document preparation
      -> three-model faithfulness + GPT support/trust
      -> scoring.py validation and arithmetic
  -> results.json + scoring-service.log + render_report()
  -> serve_reports() / browser incident selector
```

1. **Dispatch:** `cli.parser()` selects the command. `evaluate` accepts `--judge-mode replay|copilot|live`.
2. **Load:** `imports.load_manifest()` reads the case list. `bundle.build_bundle()` validates hashes, posting identity and precise cutoff, pairs calls, flags loss, and selects the initial todo. `CaseSpec`/`ResponseBundle` define the boundary.
3. **Gate:** `executor._panel()` calls all three judge roles concurrently. `scoring.gate_decision()` applies the majority rule. Failure means zero and stops; missing evidence or service errors are not invented votes.
4. **Judge eligible steps:** GPT extracts exact response claims. The executor validates bindings, prepares referenced documents and calls the faithfulness panel. References must identify supplied, eligible evidence.
5. **Calculate:** Python computes median faithfulness, discrete coverage, document age bands, source-policy ceilings, weighted step scores and mean included-step contribution. LLMs do not calculate the final score.
6. **Persist/view:** `cli.main()` writes structured results and a private runtime audit. `report.render_report()` generates compact assets; the browser only switches already-computed cases and dimension summaries.

### Invocation example that works with local synthetic data

```powershell
.\.venv\Scripts\python.exe -m scoring_service evaluate `
  --manifest data\demo-ready\cases.json `
  --config data\demo-ready\evaluation.json `
  --judge-mode replay
```

Use the output directory printed by the command with `serve --directory <output-directory>`. Replay does not invoke a live model. The demo intentionally includes unscorable/error cases and can return exit code 1 while still producing a report.

## 3) Layer/Module Responsibilities

| Module | Owns | Must not own | Evidence |
|---|---|---|---|
| CLI | Paths, case selection, run lifecycle and outputs | Semantic judge decisions | `src\scoring_service\cli.py` |
| Bundle | Temporal/identity validation, normalized evidence | Reconstructing unavailable facts | `src\scoring_service\bundle.py` |
| Executor | Gate and stage sequencing | Implicit fallback models | `src\scoring_service\executor.py` |
| Judge service | Prompt/schema construction, transport/replay, validated records | Incident writes or arbitrary tool calls | `src\scoring_service\judges.py` |
| Scorer | `35*F + 35*C + 20*T + 10*P`, normalized aggregation | Changing rules based on prose | `src\scoring_service\scoring.py` |
| Viewer | One selected incident and short dimension details | Starting a scoring run on a click | `src\scoring_service\report.py`, `assets\report.js` |

## 4) Reused Patterns

| Pattern | Where | Why |
|---|---|---|
| Typed contracts with extra fields rejected | `models.py` | Boundaries are validated instead of silently defaulting malformed input |
| Context manager | `RuntimeJournal`, HTTP clients | Close resources and retain explicit failure behavior |
| Transport injection / recorded replay | `judges.py`, `documents.py`, tests | Isolate network behavior in tests |
| Hash-bound artifacts | `bundle.py`, `judges.py`, `documents.py` | Detect changed content/configuration |
| Bounded parallel panels | `executor._panel()` | Three independent roles while retaining all votes |

## 5) Copilot SDK Backend

**Current flow if Copilot launches the command:**

```text
Copilot Chat/CLI -> local shell command -> Python -> existing HTTP or replay JudgeService
```

Launching a child process does not convert its HTTP calls into Copilot model requests.

**Implemented no-BYOK flow:**

```text
Copilot or a terminal -> Python executor -> Copilot judge adapter
  -> official Copilot SDK -> Copilot runtime using the signed-in identity
  -> explicitly selected available GPT / Claude / Gemini models
  -> existing typed validation, votes, scoring, logs and UI
```

The integration seam is `JudgeService.judge(role, stage, payload, step_id)`. Shared preparation validates model approval and request bindings. HTTP-only endpoint validation is applied only in `live` mode; `copilot` mode calls the official SDK using the signed-in identity.

The synchronous judge contract bridges the asynchronous SDK with `asyncio.run()` inside the executor's existing worker threads. Cache keys include backend, model, prompt, schema, policy and inference settings. Each request receives a new SDK `empty` session rooted under the judge cache, with `available_tools=[]`, memory/session-store disabled, no infinite-session context, and only the supplied prompt/evidence.

The existing HTTP backend remains optional. Copilot does not need separate provider deployments/keys when using its supported signed-in-user path. Exact available models, applicable data policy and usage allowance still require operator verification.

## 6) Evidence

- `src\scoring_service\__main__.py`
- `src\scoring_service\cli.py`
- `src\scoring_service\executor.py`
- `src\scoring_service\judges.py`
- `src\scoring_service\scoring.py`
- `src\scoring_service\report.py`
- Official SDK architecture/authentication: https://github.com/github/copilot-sdk and https://github.com/github/copilot-sdk/blob/main/docs/auth/authenticate.md
