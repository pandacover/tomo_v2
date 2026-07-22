# Two-Agent Worktree and Automated Release Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Give Tomo’s two local coding agents isolated per-task worktrees and automate PR validation, Daytona snapshot rollout, exact-commit Railway deployment, and production verification after changes reach `main`.

**Architecture:** Keep the current checkout as an integration/release-only `main` worktree and create agent task worktrees under a non-OneDrive directory. A small, stdlib-only Python CLI owns safe worktree creation, publication, auditing, and refusal-based cleanup. GitHub Actions validates pull requests; a separate production workflow classifies the exact `main` diff, creates a Daytona snapshot only for sandbox-image changes, updates selectors without intermediate deploys, uploads the exact checked-out commit to Railway, and verifies the resulting deployment. A repo-owned Hermes skill is the operator interface over these scripts and workflows.

**Tech Stack:** Git worktrees, Python 3.11 stdlib, `unittest`, GitHub Actions, GitHub CLI, `uv`, Bun, Railway CLI via `npx @railway/cli`, Daytona SDK, Markdown Hermes skills.

---

## Current Context and Decisions

- Repository: `C:\Users\luvma\OneDrive\Desktop\zero_labs\tomo_v2`.
- `main` currently equals `origin/main`, but the checkout has extensive unrelated dirty work and must not be used to implement this automation.
- The repository has no root `.gitignore`, no `.github/workflows/`, and no project-owned `extensions/skills/` tree.
- Thirty-seven generated Python cache files are currently tracked. Remove them only from a fresh automation branch; do not clean or reset the dirty `main` checkout.
- Six old temporary Daytona worktrees remain registered and all report dirty files. Version 1 of the worktree tool must report and refuse unsafe removal; it must never force-remove them.
- Agent worktrees default to `C:\Users\luvma\code\tomo-worktrees\`, outside OneDrive. Override with `TOMO_WORKTREE_ROOT`.
- Branches use `agent/<agent>/<task-slug>`. Neither agent works directly on `main`.
- GitHub Actions never stages or invents commits. Agents create scoped commits on their branches; PR merge is the intent boundary.
- The production workflow deploys the exact checked-out SHA with `railway up`, not `railway redeploy` of an ambiguous “latest” source.
- Railway GitHub source auto-deploy must be disabled only after the new workflow passes a dry run. Until then, `TOMO_RELEASE_AUTOMATION_ENABLED` remains unset/false.
- Snapshot-triggering inputs match `Dockerfile.daytona`: `tomo_core/src/**`, `tomo_core/SOUL.md`, `tomo_core/pyproject.toml`, `tomo_core/uv.lock`, and `tomo_core/Dockerfile.daytona`.
- A snapshot change also triggers a core deployment so the selector and control host move together.
- Core-only inputs include hosted startup/configuration such as `tomo_core/scripts/railway_core_start.py` and `tomo_core/railway.toml`.
- Dashboard inputs are under `dashboard/**`; docs, tests, plans, skills, and automation-only changes do not deploy production by themselves.
- Store only deployment credentials in the GitHub `production` environment: `RAILWAY_TOKEN`, `RAILWAY_PROJECT_ID`, and `DAYTONA_API_KEY`. Do not duplicate Telegram, Better Auth, SuperGrok, OAuth, or control API secrets into GitHub.

## Target Lifecycle

```text
agent request
  -> tools/tomo_worktree.py create
  -> isolated branch/worktree outside OneDrive
  -> scoped commits
  -> tools/tomo_worktree.py publish
  -> pull request
  -> ci.yml required gate
  -> merge to main
  -> release-production.yml classifies exact before..after diff
       -> snapshot only when Daytona inputs changed
       -> selector update with deploy suppression
       -> railway up from exact checkout
       -> exact deployment/status/health/log verification
  -> concise GitHub job summary
```

---

### Task 1: Start Implementation in an Isolated Automation Worktree

**Objective:** Ensure the automation itself does not absorb or damage the current dirty checkout.

**Files:**
- Read: `.hermes/plans/2026-07-17_145154-two-agent-worktree-ci-release.md`
- No project source edits in the current `main` worktree.

**Step 1: Recheck remote synchronization**

Run from the current repository root:

```bash
git fetch origin
git rev-list --left-right --count origin/main...main
git status --short
```

Expected: divergence is `0 0`; dirty files remain untouched.

**Step 2: Create the one-off implementation worktree manually**

```bash
python -c "from pathlib import Path; Path.home().joinpath('code','tomo-worktrees').mkdir(parents=True, exist_ok=True)"
git worktree add -b chore/worktree-release-automation \
  "$HOME/code/tomo-worktrees/worktree-release-automation" origin/main
```

Expected: a clean worktree on `chore/worktree-release-automation` outside OneDrive.

**Step 3: Prove isolation**

```bash
git -C "$HOME/code/tomo-worktrees/worktree-release-automation" status --short
git -C "$HOME/code/tomo-worktrees/worktree-release-automation" rev-parse HEAD
git rev-parse origin/main
```

Expected: empty status and identical SHAs.

---

### Task 2: Establish Repository Hygiene

**Objective:** Prevent generated caches, environments, local databases, credentials, and release archives from polluting every worktree.

**Files:**
- Create: `.gitignore`
- Delete from Git tracking only: tracked `**/__pycache__/**` and `*.py[co]`
- Test: `tools/tests/test_repository_hygiene.py`

**Step 1: Write the failing hygiene test**

Create `tools/tests/test_repository_hygiene.py` with tests that:

```python
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class RepositoryHygieneTests(unittest.TestCase):
    def test_root_gitignore_covers_generated_and_sensitive_local_state(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in (
            "__pycache__/", "*.py[cod]", ".venv/", ".pytest_cache/",
            ".ruff_cache/", ".mypy_cache/", ".tomo/", ".tomo_core/",
            ".env*", ".hermes/release-*/",
        ):
            self.assertIn(entry, text)

    def test_python_cache_files_are_not_tracked(self):
        tracked = subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, text=True
        ).splitlines()
        offenders = [
            path for path in tracked
            if "/__pycache__/" in f"/{path}" or path.endswith((".pyc", ".pyo"))
        ]
        self.assertEqual(offenders, [])
```

**Step 2: Run the test and observe failure**

```bash
python -m unittest tools.tests.test_repository_hygiene -v
```

Expected: failure because `.gitignore` is absent and 37 cache files are tracked.

**Step 3: Add conservative root ignores**

Create `.gitignore` with this baseline:

```gitignore
# Python
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.coverage
htmlcov/

# Local Tomo runtime state
.tomo/
.tomo_core/
*.sqlite
*.sqlite3

# Credentials and local configuration
.env*
!.env.example
*.pem
*.key

# Release/build artifacts
.hermes/release-*/
tomo_core/dist/*
!tomo_core/dist/.gitignore

# OS/editor
.DS_Store
Thumbs.db
```

Do not ignore `.hermes/plans/`, source skills, lockfiles, or Railway configuration.

**Step 4: Stop tracking only generated cache files**

Use an explicit generated-file list, not `git clean` or a broad reset:

```bash
git ls-files -z | python tools/remove_tracked_python_caches.py --index-only
```

If a helper is undesirable, generate the exact list, inspect it, then run `git rm --cached -- <exact paths>`. Never delete untracked user files from the dirty primary worktree.

**Step 5: Run the hygiene test**

```bash
python -m unittest tools.tests.test_repository_hygiene -v
git diff --check
```

Expected: tests pass; only `.gitignore`, the hygiene test, and cache deletions appear.

**Optional commit checkpoint, only if the user requests commits:**

```bash
git add .gitignore tools/tests/test_repository_hygiene.py
git add -u -- ':(glob)**/__pycache__/**' ':(glob)**/*.pyc' ':(glob)**/*.pyo'
git commit -m "chore(repo): establish worktree hygiene"
```

---

### Task 3: Implement Deterministic Release Classification

**Objective:** Classify a Git diff into automation-only, core, dashboard, and Daytona-snapshot release scopes using one tested source of truth.

**Files:**
- Create: `tools/release_plan.py`
- Create: `tools/tests/test_release_plan.py`

**Step 1: Write table-driven failing tests**

Cover at least these cases:

```python
CASES = [
    (["docs/daytona-railway.md"], dict(core=False, dashboard=False, snapshot=False)),
    (["tools/tomo_worktree.py"], dict(core=False, dashboard=False, snapshot=False)),
    (["dashboard/src/app/page.tsx"], dict(core=False, dashboard=True, snapshot=False)),
    (["tomo_core/scripts/railway_core_start.py"], dict(core=True, dashboard=False, snapshot=False)),
    (["tomo_core/src/tomo_core/runtime.py"], dict(core=True, dashboard=False, snapshot=True)),
    (["tomo_core/SOUL.md"], dict(core=True, dashboard=False, snapshot=True)),
    (["tomo_core/tests/test_runtime.py"], dict(core=False, dashboard=False, snapshot=False)),
    (["tomo_core/src/tomo_core/runtime.py", "dashboard/package.json"],
     dict(core=True, dashboard=True, snapshot=True)),
]
```

Also test rename paths, an all-zero Git `before` SHA, empty diffs, and newline/space-safe `-z` parsing.

**Step 2: Run and verify failure**

```bash
python -m unittest tools.tests.test_release_plan -v
```

Expected: import failure because `tools/release_plan.py` does not exist.

**Step 3: Implement pure classification first**

Use a frozen dataclass and explicit path predicates:

```python
@dataclass(frozen=True)
class ReleasePlan:
    core: bool
    dashboard: bool
    snapshot: bool


def classify_paths(paths: Iterable[str]) -> ReleasePlan:
    normalized = {path.replace("\\", "/").lstrip("./") for path in paths}
    snapshot = any(is_snapshot_input(path) for path in normalized)
    core = snapshot or any(is_core_host_input(path) for path in normalized)
    dashboard = any(path.startswith("dashboard/") for path in normalized)
    return ReleasePlan(core=core, dashboard=dashboard, snapshot=snapshot)
```

Keep path rules centralized as named tuples/functions. Do not scatter glob logic across workflow YAML.

**Step 4: Add the CLI boundary**

Support:

```bash
python tools/release_plan.py classify --base <sha> --head <sha>
python tools/release_plan.py classify --base <sha> --head <sha> --github-output "$GITHUB_OUTPUT"
```

The CLI runs `git diff --name-only -z <base>..<head>`, emits JSON to stdout for humans, and writes only `core=true|false`, `dashboard=true|false`, and `snapshot=true|false` to GitHub output. It must never print environment variables.

**Step 5: Verify**

```bash
python -m unittest tools.tests.test_release_plan -v
python tools/release_plan.py classify --base HEAD^ --head HEAD
git diff --check
```

Expected: tests pass and CLI emits a three-boolean plan.

---

### Task 4: Build Safe Worktree Creation and Audit Commands

**Objective:** Give both agents a predictable, collision-resistant way to create and inspect isolated task worktrees.

**Files:**
- Create: `tools/tomo_worktree.py`
- Create: `tools/tests/test_tomo_worktree.py`

**Step 1: Write failing unit tests around an injected Git runner**

Test:

- agent/task names are normalized to lowercase safe slugs;
- branch is `agent/<agent>/<task>`;
- default root is `Path.home() / "code" / "tomo-worktrees"`;
- `TOMO_WORKTREE_ROOT` overrides the default;
- `create` fetches `origin` and starts from `origin/main` without requiring the dirty primary checkout to be clean;
- existing branch or path causes a clear refusal;
- `doctor` parses `git worktree list --porcelain` and reports branch, HEAD, dirty count, and merged state;
- commands are passed as argument arrays, never shell-concatenated strings.

Use temporary Git repositories in integration tests; do not mock all Git behavior.

**Step 2: Run and verify failure**

```bash
python -m unittest tools.tests.test_tomo_worktree -v
```

Expected: import failure.

**Step 3: Implement `doctor`, `list`, and `create`**

CLI contract:

```text
python tools/tomo_worktree.py doctor
python tools/tomo_worktree.py list --json
python tools/tomo_worktree.py create --agent hermes --task cron-history-ui
```

`create` must:

1. resolve the repository’s common Git directory;
2. fetch `origin`;
3. require `origin/main` to exist;
4. refuse branch/path collisions;
5. create the destination parent;
6. run `git worktree add -b agent/hermes/cron-history-ui <path> origin/main`;
7. print the new path, branch, and base SHA only.

It must not copy `.env`, `.tomo*`, databases, credentials, or virtual environments.

**Step 4: Verify against a temporary real repository**

```bash
python -m unittest tools.tests.test_tomo_worktree -v
python tools/tomo_worktree.py doctor
```

Expected: tests pass; `doctor` reports the current primary and six old worktrees but changes nothing.

---

### Task 5: Add Refusal-Based Publish and Cleanup Commands

**Objective:** Automate safe branch publication and merged-worktree cleanup without inferring commit scope or deleting dirty work.

**Files:**
- Modify: `tools/tomo_worktree.py`
- Modify: `tools/tests/test_tomo_worktree.py`

**Step 1: Add failing tests for `publish`**

Require `publish` to refuse when:

- the current branch is `main` or detached;
- the worktree has staged, unstaged, or untracked files;
- there are no commits ahead of `origin/main`;
- `gh auth status` fails;
- a PR already exists but targets a different base.

On success it runs:

```text
git push --set-upstream origin HEAD
gh pr create --base main --head <branch> --fill
```

If a PR already exists for the branch, print its URL instead of creating a duplicate.

**Step 2: Add failing tests for `remove`**

`remove --branch <branch>` must refuse unless:

- the branch belongs to `agent/*`;
- its worktree is clean;
- the remote branch is merged into `origin/main` according to ancestry;
- the target path is under `TOMO_WORKTREE_ROOT`;
- the target is not the primary worktree.

There is no `--force` in version 1.

**Step 3: Implement minimal commands**

CLI contract:

```text
python tools/tomo_worktree.py publish
python tools/tomo_worktree.py remove --branch agent/hermes/cron-history-ui
```

Do not auto-stage, auto-commit, auto-merge, delete remote branches, or prune arbitrary worktrees.

**Step 4: Verify**

```bash
python -m unittest tools.tests.test_tomo_worktree -v
python tools/tomo_worktree.py doctor
```

Expected: tests pass; existing dirty temporary worktrees are reported as non-removable.

---

### Task 6: Add Pull-Request CI

**Objective:** Make one stable required GitHub check validate automation, core, and dashboard changes before merge.

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `tools/tests/test_release_plan.py`

**Step 1: Define a least-privilege workflow**

Use:

```yaml
name: ci
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
permissions:
  contents: read
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
```

Pin official actions to reviewed commit SHAs during implementation rather than floating branches. If SHA pinning is deferred, use current official major tags and record that tradeoff in the PR.

**Step 2: Add a classifier job**

Checkout with full history and call `tools/release_plan.py` using the PR base SHA or push `before` SHA. Expose the three booleans as job outputs.

**Step 3: Add stable jobs**

- `automation`: always run `python -m unittest discover -s tools/tests -v` and `git diff --check <base>..<head>`.
- `core`: when core or snapshot inputs changed, use Python 3.11 and `uv`; run from `tomo_core/`:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run python -m unittest discover -s tests
  PYTHONDONTWRITEBYTECODE=1 uv run python -c "from pathlib import Path; files=list(Path('src').rglob('*.py'))+list(Path('tests').rglob('*.py')); [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in files]"
  uv build
  ```

- `dashboard`: when dashboard inputs changed, use Bun and run from `dashboard/`:

  ```bash
  bun install --frozen-lockfile
  bun run lint
  bun run build
  ```

- `gate`: use `if: always()`, depend on all jobs, and fail unless each required job succeeded or was correctly skipped. Make only `ci / gate` a branch-protection requirement.

**Step 4: Add workflow contract tests**

In `tools/tests/test_release_plan.py`, parse the workflow as text and assert it contains:

- pull request and main push triggers;
- `permissions: contents: read`;
- the release classifier command;
- Python 3.11 full core suite;
- frozen Bun install, lint, and build;
- a final gate job;
- no production secret names.

Do not attempt to fully reimplement GitHub’s YAML validator.

**Step 5: Verify locally**

```bash
python -m unittest discover -s tools/tests -v
git diff --check
```

Expected: local contract tests pass. After pushing the branch, GitHub’s parser and CI run are the authoritative workflow validation.

---

### Task 7: Build a Secret-Safe Railway Release Helper

**Objective:** Encapsulate Railway JSON parsing, exact deployment tracking, health checks, and sanitized log verification in a tested Python CLI.

**Files:**
- Create: `tools/railway_release.py`
- Create: `tools/tests/test_railway_release.py`

**Step 1: Write failing parser and state-machine tests**

Test pure functions for:

- extracting a deployment ID from line-delimited `railway up --detach --json` output;
- finding one exact deployment in `railway deployment list --json`;
- accepting `SUCCESS` only and rejecting `FAILED`, `CRASHED`, `REMOVED`, or timeout;
- parsing only `TOMO_DAYTONA_SNAPSHOT` from a variable payload while never returning/logging other values;
- requiring HTTP 200 and the actual core contract `{"ok":"true"}`;
- requiring core startup markers:
  - `starting control api and shared telegram listener.`
  - `shared telegram gateway polling started.`
  - `Application startup complete.`
- rejecting tracebacks, uncaught exceptions, and startup failures;
- dashboard health accepting a 2xx `/` response;
- command errors returning sanitized messages without stdout/stderr that may contain secrets.

**Step 2: Run and verify failure**

```bash
python -m unittest tools.tests.test_railway_release -v
```

Expected: import failure.

**Step 3: Implement narrow subcommands**

```text
python tools/railway_release.py selector get --service core
python tools/railway_release.py selector set --service core --snapshot tomo-core-abcdef1
python tools/railway_release.py deploy --service core --path tomo_core --sha <full-sha>
python tools/railway_release.py deploy --service dashboard --path dashboard --sha <full-sha>
python tools/railway_release.py verify-core --deployment <id> --sha <full-sha>
python tools/railway_release.py verify-dashboard --deployment <id> --sha <full-sha>
```

Every Railway invocation includes explicit project, environment, and service arguments sourced from environment variables. Use subprocess argument arrays and `shell=False`.

`deploy` must run from the exact checked-out service directory:

```text
npx --yes @railway/cli up --detach --json --yes \
  --project <id> --environment production --service <service> \
  --message github:<full-sha>
```

Capture the returned deployment ID, poll only that ID with a bounded deadline, and print a sanitized summary.

**Step 4: Implement rollback preparation, not magical rollback**

Before changing a snapshot selector, retain the previous non-secret snapshot name. If snapshot creation or core deployment fails before success, restore that selector with deploy suppression. Do not claim code rollback support: Railway’s current CLI redeploy command targets the latest deployment and cannot select an arbitrary historic deployment.

Railway keeps the previous successful deployment serving while a new deployment fails. The workflow should stop, restore the selector for the next attempt, and report the exact failed deployment ID.

**Step 5: Verify**

```bash
python -m unittest tools.tests.test_railway_release -v
python -m unittest discover -s tools/tests -v
git diff --check
```

Expected: all helper tests pass with no network access.

---

### Task 8: Add the Production Release Workflow

**Objective:** Automatically release a verified `main` commit with path-aware snapshot creation and exact-SHA Railway uploads.

**Files:**
- Create: `.github/workflows/release-production.yml`
- Modify: `tools/tests/test_release_plan.py`
- Modify: `tools/tests/test_railway_release.py`

**Step 1: Define guarded triggers and concurrency**

```yaml
name: release-production
on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      dry_run:
        type: boolean
        default: true
      force_core:
        type: boolean
        default: false
      force_dashboard:
        type: boolean
        default: false
permissions:
  contents: read
concurrency:
  group: production-release
  cancel-in-progress: false
```

The production job uses `environment: production` and runs only when:

- the push is to `main` and repository variable `TOMO_RELEASE_AUTOMATION_ENABLED == 'true'`; or
- a maintainer manually dispatches it.

A manual dispatch must still check out `main`’s selected SHA; do not accept arbitrary untrusted refs.

**Step 2: Re-run release gates on the exact SHA**

Do not rely only on a previous PR run. Checkout `github.sha`, classify `github.event.before..github.sha`, and run the same core/dashboard/automation commands as CI for the selected release scope.

**Step 3: Create the snapshot before mutating Railway**

When `snapshot=true`:

```bash
SNAPSHOT="tomo-core-${GITHUB_SHA::7}"
cd tomo_core
DAYTONA_API_KEY="${{ secrets.DAYTONA_API_KEY }}" \
  uv run python scripts/create_daytona_snapshot.py --name "$SNAPSHOT"
```

Requirements:

- never use `--replace`;
- an already-active exact immutable name is treated as idempotent success only after querying that exact snapshot;
- a failed/non-active snapshot stops the release;
- snapshot creation happens from the clean GitHub checkout, never a dirty local directory.

**Step 4: Switch the selector atomically**

When snapshot creation succeeds:

1. capture the current non-secret selector through `tools/railway_release.py`;
2. set `TOMO_DAYTONA_SNAPSHOT=tomo-core-<sha>` with `--skip-deploys`;
3. read back only that selector and assert equality;
4. upload core once from `tomo_core/`.

Do not list or print the complete Railway variable JSON in Actions logs.

**Step 5: Deploy exact scopes**

- Core: upload `tomo_core/` with the helper and verify exact deployment ID, `SUCCESS`, health payload, startup markers, and absence of startup failures.
- Dashboard: upload `dashboard/` and verify exact deployment ID plus a 2xx `/` health response.
- Mixed changes: create snapshot once, then deploy core and dashboard as separate, explicitly tracked deployments.
- Docs/automation-only changes: produce a “no production deployment required” job summary.

**Step 6: Add a useful job summary**

Write only non-secret fields to `$GITHUB_STEP_SUMMARY`:

- full commit SHA;
- release classification;
- snapshot name/state when applicable;
- Railway service and deployment IDs;
- deployment terminal statuses;
- health results;
- whether old selector was restored after failure.

**Step 7: Add workflow contract tests**

Assert the workflow includes:

- production environment;
- non-cancelling release concurrency;
- automation-enabled repository-variable gate;
- no arbitrary ref input;
- snapshot-before-selector-before-core-deploy ordering;
- `--skip-deploys` selector mutation;
- exact-SHA Railway upload;
- bounded exact-deployment verification;
- no command that prints all Railway values;
- no `git add`, `git commit`, `git clean`, force push, snapshot `--replace`, or volume deletion.

**Step 8: Verify locally**

```bash
python -m unittest discover -s tools/tests -v
git diff --check
```

Expected: all automation and workflow contract tests pass.

---

### Task 9: Create the Repo-Owned Hermes Skill

**Objective:** Make both agents consistently use the worktree and automated-release workflow instead of modifying `main` or manually duplicating deployment steps.

**Files:**
- Create: `extensions/skills/tomo-worktree-release/SKILL.md`
- Create: `extensions/skills/tomo-worktree-release/references/release-contract.md`
- Create: `tools/tests/test_repo_skill.py`

**Step 1: Write the failing skill contract test**

Validate:

- frontmatter starts at byte zero and includes `name`, trigger-focused `description`, version, author, license, tags, and related skills;
- name is `tomo-worktree-release`;
- description is at most 1024 characters;
- body is non-empty and under 100,000 characters;
- it references `tools/tomo_worktree.py`, `ci / gate`, and `release-production`;
- it explicitly forbids direct work on `main`, broad staging, force cleanup, manual duplicate deploys, and secret output;
- it explains that skill discovery requires a fresh Hermes project session after installation.

**Step 2: Run and verify failure**

```bash
python -m unittest tools.tests.test_repo_skill -v
```

Expected: missing skill failure.

**Step 3: Write the compact primary skill**

Use frontmatter similar to:

```yaml
---
name: tomo-worktree-release
description: Use when starting, publishing, merging, or releasing Tomo work across multiple local coding agents. Creates isolated task worktrees, preserves dirty user work, routes publication through PR validation, and delegates production rollout to the exact-SHA GitHub release workflow.
version: 1.0.0
author: Tomo
license: MIT
metadata:
  hermes:
    tags: [Tomo, Git, Worktrees, CI-CD, Railway, Daytona]
    related_skills: [github-pr-workflow, tomo-tight-release]
---
```

The body should define:

1. trigger conditions;
2. `doctor` before allocation;
3. `create --agent --task`;
4. agent ownership and explicit path scope;
5. local verification and scoped commits;
6. `publish` and PR check monitoring;
7. no manual deployment while `release-production` owns the SHA;
8. production completion criteria;
9. safe `remove` after merge;
10. failure recovery and escalation.

Keep workflow implementation details in the linked reference rather than duplicating YAML.

**Step 4: Write the release contract reference**

Document:

- release classification table;
- required checks;
- required GitHub secrets/variables;
- sensitive-data boundaries;
- Daytona snapshot and selector ordering;
- exact deployment verification;
- refusal and rollback semantics;
- one-page operator command recipes.

**Step 5: Verify**

```bash
python -m unittest tools.tests.test_repo_skill -v
python -m unittest discover -s tools/tests -v
```

Expected: skill validation passes. Confirm discovery in a fresh Hermes project session; do not expect the current cached skill catalog to update.

---

### Task 10: Document the Human and Agent Operating Model

**Objective:** Give the user one concise runbook for daily two-agent work and one-time CI/CD setup.

**Files:**
- Create: `docs/worktree-release-workflow.md`
- Modify: `docs/daytona-railway.md`

**Step 1: Document daily commands**

Include:

```bash
python tools/tomo_worktree.py doctor
python tools/tomo_worktree.py create --agent hermes --task <slug>
# agent edits/tests/commits inside printed path
python tools/tomo_worktree.py publish
# merge after ci / gate succeeds
python tools/tomo_worktree.py remove --branch agent/hermes/<slug>
```

Explain that the current root checkout becomes release-only after existing dirty work is migrated, committed, or intentionally preserved elsewhere.

**Step 2: Document task ownership rules**

- one task branch per agent assignment;
- no permanent long-lived “agent branch” unless explicitly needed;
- divide overlapping backend/dashboard scopes before starting;
- merge/rebase conflicts are integration work, not solved by sharing a filesystem;
- mutable runtime directories are never shared across worktrees;
- `.venv` and `node_modules` remain per-worktree while uv/Bun caches are shared by their package managers.

**Step 3: Document GitHub setup**

Create GitHub environment `production` with:

Secrets:

```text
RAILWAY_TOKEN
RAILWAY_PROJECT_ID
DAYTONA_API_KEY
```

Variables:

```text
RAILWAY_ENVIRONMENT=production
RAILWAY_CORE_SERVICE=core
RAILWAY_DASHBOARD_SERVICE=dashboard
CORE_HEALTH_URL=https://core-production-535d.up.railway.app/v1/health
DASHBOARD_HEALTH_URL=<canonical dashboard URL>
TOMO_RELEASE_AUTOMATION_ENABLED=false
```

Recommend an environment reviewer during initial rollout. Fully unattended deployment is achieved later by removing the reviewer after confidence, not by weakening workflow checks.

Do not place Telegram, Better Auth, SuperGrok OAuth, control API, or user-data secrets in GitHub.

**Step 4: Update the Daytona runbook**

Point manual release instructions to the automated workflow while retaining manual recovery commands. State that GitHub Actions is the normal deployment owner after cutover and operators must not start a duplicate local snapshot/deploy for the same SHA.

**Step 5: Verify documentation**

```bash
python -m unittest discover -s tools/tests -v
git diff --check
```

Expected: no contradictory snapshot or deployment ownership instructions.

---

### Task 11: Validate the Complete Branch Before Publication

**Objective:** Prove the automation branch is self-contained and does not depend on the dirty primary checkout.

**Files:**
- Verify all files from Tasks 2–10.

**Step 1: Run automation tests**

```bash
python -m unittest discover -s tools/tests -v
```

Expected: all worktree, classifier, Railway helper, hygiene, and skill tests pass.

**Step 2: Run existing core gates**

```bash
cd tomo_core
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run python -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 uv run python -c "from pathlib import Path; files=list(Path('src').rglob('*.py'))+list(Path('tests').rglob('*.py')); [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in files]"
uv build
```

Expected: full suite, compilation, and build pass.

**Step 3: Run dashboard gates**

```bash
cd dashboard
bun install --frozen-lockfile
bun run lint
bun run build
```

Expected: all pass without production secrets.

**Step 4: Inspect exact diff and generated-file state**

```bash
git status --short
git diff --check
git ls-files | python -c "import sys; bad=[p.strip() for p in sys.stdin if '/__pycache__/' in '/' + p or p.strip().endswith(('.pyc','.pyo'))]; print('\n'.join(bad)); raise SystemExit(bool(bad))"
```

Expected: every changed path belongs to this plan; no tracked Python cache remains.

**Step 5: Test the proposed commit tree**

Stage only the explicit automation paths, inspect the staged patch, archive `git write-tree` into a temporary directory, and rerun `tools/tests`, core tests/build, and dashboard build there. This proves the commit does not rely on unstaged files.

**Optional commit checkpoint, only if the user requests commits:**

```bash
git commit -m "ci: add isolated agent worktrees and automated releases"
```

---

### Task 12: Stage the Remote Cutover Safely

**Objective:** Enable automation without causing duplicate Railway deployments or risking an untested production path.

**Files:**
- Remote GitHub/Railway settings only after explicit user authorization.
- No source changes expected unless live validation finds a defect.

**Step 1: Publish through the new branch workflow**

Push the automation branch and open a PR. Verify `ci / gate` on GitHub. Do not enable production automation yet.

**Step 2: Configure branch protection**

Require PRs for `main` and require `ci / gate`. Decide whether the user wants merge queue, squash merge, and environment approval; document the chosen settings.

**Step 3: Configure the GitHub production environment**

Add only the secrets/variables listed in Task 10. Keep `TOMO_RELEASE_AUTOMATION_ENABLED=false`.

**Step 4: Run a manual dry run**

Dispatch `release-production` with `dry_run=true`. It must:

- checkout current `main`;
- classify the diff;
- authenticate to Railway/Daytona without printing values;
- run verification;
- make no snapshot, selector, or deployment mutation.

Expected: green workflow and sanitized summary.

**Step 5: Perform one staging/canary deployment**

Prefer a Railway staging environment/service. Capture the real `railway up --detach --json` shape as a redacted test fixture and update parser tests if required. Prove exact deployment polling and health verification before production cutover.

If no staging environment exists, stop and ask for explicit authorization before using production as the canary.

**Step 6: Transfer deployment ownership**

Only after the dry run/canary passes:

1. disable Railway GitHub source auto-deploy for core and dashboard;
2. set `TOMO_RELEASE_AUTOMATION_ENABLED=true`;
3. manually dispatch one release from current `main` with explicit core/dashboard force flags as appropriate;
4. verify no duplicate Railway deployments were created.

**Step 7: Verify the first production run**

Completion requires:

- GitHub workflow identifies the exact `main` SHA;
- any required `tomo-core-<sha>` snapshot is active;
- Railway selector equals that snapshot;
- exact core/dashboard deployment IDs reach `SUCCESS`;
- core health is HTTP 200 with `{"ok":"true"}`;
- core logs show API and shared Telegram startup with no startup failures;
- dashboard health is 2xx when deployed;
- no secret values appear in Actions logs;
- existing Daytona user volumes remain untouched and reconcile lazily on the next turn;
- the primary dirty checkout and old dirty worktrees remain unmodified.

---

## Files Likely to Change

```text
.gitignore
.github/workflows/ci.yml
.github/workflows/release-production.yml
tools/release_plan.py
tools/railway_release.py
tools/tomo_worktree.py
tools/remove_tracked_python_caches.py
tools/tests/test_repository_hygiene.py
tools/tests/test_release_plan.py
tools/tests/test_railway_release.py
tools/tests/test_tomo_worktree.py
tools/tests/test_repo_skill.py
extensions/skills/tomo-worktree-release/SKILL.md
extensions/skills/tomo-worktree-release/references/release-contract.md
docs/worktree-release-workflow.md
docs/daytona-railway.md
```

Tracked `tomo_core/**/__pycache__/**` and `*.pyc` files will be removed from Git in the isolated automation branch. No user database, runtime directory, credential file, untracked source file, or old worktree may be deleted by this implementation.

## Test Matrix

| Area | Command | Required result |
| --- | --- | --- |
| automation unit/integration | `python -m unittest discover -s tools/tests -v` | all pass |
| core | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run python -m unittest discover -s tests` | all pass |
| Python compilation | no-write `compile(...)` over `src/` + `tests/` | no errors |
| wheel | `uv build` | wheel builds and packaged skills remain present |
| dashboard install | `bun install --frozen-lockfile` | lockfile unchanged |
| dashboard lint | `bun run lint` | pass |
| dashboard build | `bun run build` | pass |
| Git whitespace | `git diff --check` and staged equivalent | no errors |
| staged tree | archive `git write-tree`; rerun all relevant gates | pass without unstaged files |
| PR workflow | `ci / gate` | success |
| dry run | manual release workflow with `dry_run=true` | no remote mutations |
| canary | staging exact-SHA upload | exact deployment succeeds and health passes |
| production | first enabled release | one deployment per affected service; all final gates pass |

## Risks and Tradeoffs

1. **Current dirty primary checkout:** Worktrees solve future overlap but do not automatically sort existing changes. Leave current work untouched and implement from `origin/main` in a new worktree.
2. **Old dirty worktrees:** Automatic force cleanup could destroy recoverable work. Version 1 only reports/refuses; cleanup is a separate user-approved operation after inspection.
3. **Tracked cache deletion:** Removing tracked `.pyc` files is correct hygiene but may create apparent changes in old worktrees. Do it in the automation branch and never clean those worktrees implicitly.
4. **Duplicate Railway ownership:** GitHub Actions and Railway source auto-deploy cannot both own releases. Use the guarded cutover variable and disable source auto-deploy only after canary success.
5. **No arbitrary Railway rollback:** Current CLI cannot redeploy a selected historical deployment. The workflow restores the old snapshot selector after a failed rollout and relies on Railway retaining the previous successful deployment. Manual semantic rollback remains an operator action.
6. **Snapshot build time:** Snapshot creation remains the slowest conditional step, but it runs only when exact Docker inputs change.
7. **Concurrent overlapping tasks:** Worktrees prevent filesystem corruption, not semantic merge conflicts. Agents still need non-overlapping scopes or explicit integration work.
8. **GitHub action supply chain:** Prefer SHA-pinned official actions. Review and update pins deliberately rather than using floating third-party actions.
9. **Production approval:** Start with a GitHub environment reviewer. Remove that reviewer only if the user explicitly chooses fully unattended production deployment.
10. **Skill discovery caching:** The new repo-owned skill appears only in a fresh Hermes project session after merge.

## Open Questions for Cutover, Not Implementation

- What is the canonical production dashboard health URL?
- Is a Railway staging environment available for the first exact-SHA upload test?
- Should production keep a required GitHub environment reviewer, or become fully automatic after canary success?
- Should PRs squash-merge or preserve agent commits?
- After auditing the six old dirty worktrees, which contain unique work worth preserving? The automation must not decide this itself.

## Definition of Done

- Both agents can create isolated task worktrees outside OneDrive with one command.
- Worktrees cannot be silently overwritten or force-removed.
- `main` is protected and used only as integration/release state.
- PRs cannot merge without one stable CI gate.
- A `main` push automatically classifies release scope.
- Daytona snapshots are created only for actual sandbox inputs and are named from the exact commit.
- Railway receives the exact checked-out core/dashboard source, not an ambiguous latest deployment.
- Deployment IDs, selectors, health, and startup logs are verified without exposing secrets.
- Docs and a repo-owned Hermes skill make the same workflow repeatable for both agents.
- Existing dirty user work and persistent Tomo/Daytona data remain untouched.
