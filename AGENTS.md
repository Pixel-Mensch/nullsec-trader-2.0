# AGENTS.md — AI Agent Workflow Guide

## Mandatory Read Order (before touching any code)

1. `AGENTS.md` ← this file
2. `PROJECT_STATE.md`
3. `TASK_QUEUE.md`
4. `ARCHITECTURE.md`
5. `SESSION_HANDOFF.md`
6. `README.md` (when it exists)
7. `.github/copilot-instructions.md` (when doing Copilot/IDE work)

Do not skip this read order. These files exist to prevent wasted tokens and contradictory changes.

---

## Branch Workflow

| Branch | Purpose |
|--------|---------|
| `main` | Stable, deployable state only |
| `dev` | Default working branch — all development happens here |
| `feature/*` | Optional, only when a change is structurally isolated |

**Default rule:** work on `dev`. Only create a feature branch if there is a clear structural reason (e.g., risky refactor, parallel workstreams). Never commit directly to `main` unless the repo is fully stable and the task is complete.

---

## Reading Strategy

- Start with root file listing + the control files above.
- Do **not** perform a full repository scan unless a task explicitly requires it.
- Read only the files directly relevant to the current task.
- If a file's purpose is unclear, note the uncertainty — do not invent facts.

---

## Change Discipline

- Make small, focused, logically separated commits.
- Do not refactor or rewrite code unrelated to the current task.
- Do not add features beyond what is explicitly requested.
- Do not remove code unless it is provably unused and the task requires cleanup.
- If you find a bug while working on something else, record it in `TASK_QUEUE.md` and leave it.

---

## Documentation Updates

After any meaningful change:
- Update `PROJECT_STATE.md` if the project state changed.
- Update `TASK_QUEUE.md` to reflect completed or new tasks.
- Update `SESSION_HANDOFF.md` so the next agent has accurate context.
- Update `ARCHITECTURE.md` if structure or entry points changed.

---

## Testing Expectations

- Run existing tests before committing if a test runner is configured.
- Do not remove or skip tests.
- If tests are not yet configured, note this as a gap in `TASK_QUEUE.md`.
- Add tests when adding new logic, unless the task scope explicitly excludes it.

---

## Security Rules

- Never commit secrets, API keys, tokens, or credentials.
- If secrets are found hardcoded, record the risk in `PROJECT_STATE.md` under Known Issues — do not print the values.
- Use environment variables or a secrets manager for all sensitive configuration.
- Do not add `.env` files to version control (ensure `.gitignore` covers them).

---

## Handoff Requirement

Every session must end with `SESSION_HANDOFF.md` updated to reflect:
- What was done
- Current repo state
- What should happen next
- Relevant files for the next session
- Any warnings or assumptions

The repository must be in a committable, coherent state before the session ends.
