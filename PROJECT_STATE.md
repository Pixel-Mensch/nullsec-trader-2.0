# PROJECT_STATE.md

## Project Purpose

**nullsec-trader 2.0** — EVE Online Nullsec Price Scanner (Performance Edition).

Compares Jita (The Forge) sell prices with configured nullsec structure market prices to identify profitable import trade opportunities. Accounts for broker fees, sales tax, and shipping costs. Filters by minimum profit (ISK and %) and minimum daily volume.

v2.0 is a performance rewrite: bulk ESI fetches + parallel ThreadPoolExecutor replace per-item API calls, reducing runtime from ~3 minutes to <30 seconds.

---

## Current State

- **Phase:** Working prototype — single-file script, functional but needs configuration
- **Tech stack:** Python 3, `requests` (only external dependency)
- **Entry point:** `price_scanner.py` (616 lines)
- **Git:** Initialized 2026-03-16, on `dev` branch
- **GitHub remote:** https://github.com/Pixel-Mensch/nullsec-trader-2.0

---

## Key Modules / Areas (within `price_scanner.py`)

| Section | Lines (approx) | Responsibility |
|---------|----------------|---------------|
| Configuration | 38–93 | CLIENT_ID, structure IDs, shipping rates, skill levels, filters |
| Fee calculation | 99–135 | Sales tax, broker fees, shipping cost, net profit |
| EVE SSO | 141–226 | OAuth2 login, token refresh, disk cache (`cache/sso_token.json`) |
| ESI helpers | 232–270 | Paginated and single ESI API calls |
| Bulk fetchers | 276–352 | Jita orders (bulk), Jita history (parallel), nullsec history (parallel) |
| Item cache | 358–394 | Disk-backed item name+volume cache (`cache/item_info.json`) |
| Region resolution | 400–415 | Structure ID → region ID via ESI |
| Scanner | 421–524 | Main scan loop per structure, deal filtering |
| Output | 530–578 | Console formatting, top-20 deals, per-structure summary |
| Main | 584–616 | Entry point, auth, cache init, scan, JSON export |

---

## Completed Work

- [2026-03-16] Repository initialized with Git and control files
- [prior] `price_scanner.py` — fully functional performance-edition price scanner written

---

## Known Issues

1. **CLIENT_ID is hardcoded** in the script as a placeholder (`"DEINE_CLIENT_ID_HIER"`) — must be replaced by the user. Security risk: if someone accidentally commits their real CLIENT_ID, it will be exposed.
2. **Structure IDs are placeholder values** (`1234567890123` etc.) — must be replaced with real structure IDs.
3. **`cache/sso_token.json` stores OAuth tokens on disk** — this directory must be in `.gitignore` to prevent credential exposure.
4. **No `.gitignore`** — `cache/` and `scan_result_*.json` output files could be accidentally committed.
5. **No `requirements.txt`** — dependency (`requests`) is undocumented.
6. **No README** — project has no setup or usage documentation.
7. **No tests** — no test framework configured.
8. **Script is German-language** — comments, print statements, and variable conventions are in German. Consider whether English is needed for collaborators.

---

## Current Focus

Harden the project setup: add `.gitignore`, `requirements.txt`, README, and move CLIENT_ID to an environment variable or config file.

---

## Risks and Uncertainties

- EVE SSO tokens cached to disk (`cache/sso_token.json`) — must never be committed
- CLIENT_ID hardcoded — accidental commit exposure risk
- ESI API rate limits apply; no retry/backoff logic is currently implemented
- `PARALLEL_WORKERS = 10` — may hit ESI rate limits under heavy use
