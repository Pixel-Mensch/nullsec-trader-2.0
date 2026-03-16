# TASK_QUEUE.md

Status: `[ ]` pending · `[~]` in progress · `[x]` done · `[!]` blocked

---

## Priority 1 — Security & Hygiene (do first, before any code work)

| # | Task | Status | Relevant Files | Expected Outcome | Notes |
|---|------|--------|---------------|-----------------|-------|
| 1 | Add `.gitignore` | `[ ]` | `.gitignore` | `cache/`, `scan_result_*.json`, `.env`, `__pycache__/`, `*.pyc` excluded | **Critical** — `cache/sso_token.json` contains OAuth tokens |
| 2 | Move `CLIENT_ID` out of source code | `[ ]` | `price_scanner.py`, `.env`, `.env.example` | CLIENT_ID loaded from env var `EVE_CLIENT_ID`; `.env.example` documents the variable | Prevents accidental credential exposure in git |
| 3 | Add `requirements.txt` | `[ ]` | `requirements.txt` | `requests` (and version pin) documented | Currently undocumented dependency |

---

## Priority 2 — Documentation

| # | Task | Status | Relevant Files | Expected Outcome | Notes |
|---|------|--------|---------------|-----------------|-------|
| 4 | Create `README.md` | `[ ]` | `README.md` | Setup steps, usage, configuration reference, EVE Developer App registration | Currently a project gap |
| 5 | Add inline comments for non-obvious configuration options | `[ ]` | `price_scanner.py` | Config section self-documenting | Many magic numbers (0.006, 0.999, etc.) need explanation |

---

## Priority 3 — Robustness

| # | Task | Status | Relevant Files | Expected Outcome | Notes |
|---|------|--------|---------------|-----------------|-------|
| 6 | Add ESI retry/backoff logic | `[ ]` | `price_scanner.py` (ESI helpers) | Transient ESI errors handled gracefully | ESI is unreliable; no retry exists now |
| 7 | Add ESI rate-limit awareness | `[ ]` | `price_scanner.py` | Respect `X-ESI-Error-Limit-Remain` header | `PARALLEL_WORKERS=10` may trigger rate limits |
| 8 | Validate structure IDs on startup | `[ ]` | `price_scanner.py` (`main()`) | Clear error if all structure IDs are placeholders | Currently silently skips placeholder IDs |

---

## Priority 4 — Testing

| # | Task | Status | Relevant Files | Expected Outcome | Notes |
|---|------|--------|---------------|-----------------|-------|
| 9 | Add test framework (pytest) | `[ ]` | `tests/`, `requirements-dev.txt` | `pytest` runs without errors | No tests exist yet |
| 10 | Unit tests for fee calculation functions | `[ ]` | `tests/test_fees.py` | `calc_net_profit`, `calc_sales_tax`, etc. covered | Pure functions — easy to test with known inputs |
| 11 | Unit tests for deal filtering logic | `[ ]` | `tests/test_scanner.py` | Filtering by profit/volume thresholds tested with mock data | |

---

## Priority 5 — Future Features

| # | Task | Status | Relevant Files | Expected Outcome | Notes |
|---|------|--------|---------------|-----------------|-------|
| 12 | Multi-character / multi-account support | `[ ]` | TBD | Multiple SSO tokens managed | Nice-to-have for corp traders |
| 13 | Scheduled / automated runs | `[ ]` | TBD | Script can run on a timer without interactive login | Requires refresh token persistence (already partially done) |
| 14 | Web UI or richer output format | `[ ]` | TBD | HTML report or dashboard | Currently CLI-only |

---

## Completed

| # | Task | Completed | Notes |
|---|------|-----------|-------|
| 0 | Repository initialized with control files | 2026-03-16 | Git init + all control files created |
| — | `price_scanner.py` v2.0 written | prior | Performance-edition scanner functional |
