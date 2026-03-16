# Copilot Instructions — nullsec-trader 2.0

## Read These First (in order)

1. `AGENTS.md` — workflow rules
2. `PROJECT_STATE.md` — current status and known issues
3. `TASK_QUEUE.md` — what needs doing and why
4. `ARCHITECTURE.md` — code structure with line references
5. `SESSION_HANDOFF.md` — what just happened and what's next

## Branch

Work on `dev`. Do not touch `main` until a stable milestone is confirmed.

## Change Rules

- Small, focused changes only. One logical change per commit.
- Do not refactor code unrelated to the current task.
- Do not add features beyond what is explicitly requested.
- If you find a bug while working on something else, record it in `TASK_QUEUE.md`.

## After Any Meaningful Change

Update `PROJECT_STATE.md`, `TASK_QUEUE.md`, and `SESSION_HANDOFF.md` to reflect the new state.

## Security

- Never commit `.env`, `cache/sso_token.json`, or `scan_result_*.json`
- Never commit real EVE CLIENT_IDs, tokens, or credentials
- `CLIENT_ID` must come from environment variable `EVE_CLIENT_ID`, not hardcoded

## Testing

- Run `pytest` before committing if tests exist
- Add tests for new logic in `tests/`
- Required dependency: `pip install -r requirements.txt` (+ `requirements-dev.txt` for tests)

## Key File

All application logic is in `price_scanner.py`. See `ARCHITECTURE.md` for section line references.
