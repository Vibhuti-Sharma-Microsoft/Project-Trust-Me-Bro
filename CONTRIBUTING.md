# Contributor workflow

## First checkout

Run the README's local setup, tests and synthetic replay before configuring any live service. Real data and credentials are not required to contribute.

The standalone project is the repository boundary. Do not initialize or push its parent workspace, which may contain unrelated repositories and private exports.

## Two-person ownership

| Person | Primary track | Owned areas |
|---|---|---|
| **A: data and evidence** | Collect the real corpus; verify response identity/cutoffs; normalize sources and document metadata | `corpus.py`, `imports.py`, `bundle.py`, `time_utils.py`, `documents.py`, `tools`, corresponding tests, private data |
| **B: judges and evaluation** | Configure approved models; refine judgments/policies; review scores and present results | `judges.py`, `prompts`, `scoring.py`, `executor.py`, `runtime_log.py`, `report.py`, templates/assets, corresponding tests |
| **Joint review** | Data/result contracts, CLI boundaries, policy decisions and release readiness | `models.py`, `config.py`, `cli.py`, `pyproject.toml`, constraints, README and CI |

A and B are ownership roles, not GitHub usernames. Add actual handles to CODEOWNERS only after the repository and accounts are known.

### Avoid merge conflicts

1. Open a small task using the task template and assign one primary owner.
2. Agree on any shared contract change first: fields, example JSON, error states and acceptance tests. Put that contract change in a separate PR.
3. Work in short-lived branches, such as `data/real-corpus`, `eval/judge-endpoints` or `ui/claim-drilldown`.
4. Change only your owned area unless both people agree. Use a synthetic fixture to unblock the other track rather than waiting for credentials.
5. The other person reviews each PR. Check the evidence contract as well as the code.
6. Merge green, reviewed changes to `main`; rebase/update the other branch before integration.

No raw incident data belongs in PRs, issues, screenshots or public artifacts. Exchange the private roster/exports only through an approved internal location, with stable case IDs and content hashes.

## Remaining-work coordination

Use [TASKS.md](TASKS.md) as the initial backlog. Create issues with the same task IDs and labels such as `data`, `eval`, `ui` and `blocked-external`; do not put confidential IDs in issue titles.

A GitHub Project with **Backlog / Ready / In progress / Review / Done** is sufficient. Keep one primary active task per person; surface external blockers explicitly. A working boundary is:

```text
Person A: verified ResponseBundle + EvidenceItems + document metadata
                            |
                 synthetic contract fixture
                            |
Person B: judge decisions -> StepResults -> CaseResult -> HTML
```

Treat changes to weights, bands, applicability or missing-evidence handling as versioned policy changes with joint approval, not prompt-only tweaks.

## Definition of done

- Relevant unit/integration tests and type checking pass.
- No required evidence silently disappears from a denominator.
- Gate failure, missing input and model/service failure remain distinguishable.
- Future evidence cannot justify a previous response.
- Judge/model/prompt/policy provenance and source references remain inspectable.
- HTML treats every incident/model/document value as untrusted text.
- The scorecard remains minimal; full analysis belongs in the separate private runtime log.
- Incident selection and dimension dialogs are covered by browser checks when UI behavior changes.
- No real data, endpoint credentials, tokens or generated private reports are added to Git.
- README/help and task status reflect the behavior.

## Safe initial GitHub push

Prefer an organization-approved **private** repository. Public publication needs the organization's approval and an explicit license decision; no license has been selected automatically.

From this standalone directory, inspect what would be published:

```powershell
git status --short
git add --dry-run .
git status --short --ignored
.\.venv\Scripts\python.exe tools\check_publish.py
```

The ignored paths must include `data`, `.cache`, `out`, `.venv`, `.env*` and local configuration. The original developer's private incident roster will therefore not appear in a fresh clone; transfer it separately through the approved channel.

The publish check also inspects staged content, so a sanitized working tree cannot hide an older staged value. It blocks known private paths and the local roster's incident IDs; it is not a general secret scanner and does not replace manual review or GitHub push protection.

After reviewing the candidate files, create the initial commit and connect the repository you created:

```powershell
git add .
git diff --cached --stat
git diff --cached
git commit -m "Initial local evidence-scoring prototype"
git remote add origin <YOUR_APPROVED_PRIVATE_REPOSITORY_URL>
git push -u origin main
```

No remote repository, commit, or push is performed by the preparation workflow. If `origin` already exists, inspect it instead of replacing it.

Enable branch protection for `main`, require the CI check and one review, and enable the repository's secret-scanning/push-protection features if available.

## Dependency updates

`requirements-dev.lock` pins the versions used by the current development/test environment. Install with it as a constraints file alongside `pyproject.toml`.

Update dependencies in an isolated environment, rerun tests/type checking, and regenerate using `python -m pip freeze --exclude-editable`. Review the diff and never include editable machine paths, credentials or private registry URLs. CI validates platform compatibility; these pins are not a claim of a fully hermetic build.
