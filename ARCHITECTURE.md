# ARCHITECTURE.md

## High-Level Structure

**nullsec-trader 2.0** is a single-file Python CLI tool that scans EVE Online market data to find profitable Jita → Nullsec import trades.

```
[EVE SSO OAuth2] → [access_token]
        ↓
[ESI API: Jita bulk orders + history (parallel)]
        ↓
[ESI API: Nullsec structure orders + regional history (parallel)]
        ↓
[Fee + profit calculation per item per structure]
        ↓
[Filter: MIN_PROFIT_ISK, MIN_PROFIT_PCT, MIN_DAILY_VOL]
        ↓
[Console output: top 20 deals + per-structure summary]
        ↓
[JSON export: scan_result_{timestamp}.json]
```

---

## Entry Point

```
python price_scanner.py
```

Single executable script. No CLI arguments — all configuration is at the top of the file.

---

## Main Modules (all in `price_scanner.py`)

### Configuration (lines 38–93)
All user-editable settings live here. Must be configured before first run:
- `CLIENT_ID` — EVE Developer App client ID (currently a placeholder, **security risk if hardcoded**)
- `STRUCTURES` — dict of structure name → structure ID for nullsec markets to scan
- `SHIPPING` — shipping cost parameters per logistics service (HWL, ITL)
- `SKILLS` — character skill levels affecting fee calculations
- Filter thresholds: `MIN_PROFIT_ISK`, `MIN_PROFIT_PCT`, `MIN_DAILY_VOL_JITA`, `MIN_DAILY_VOL_NULL`

### Fee Calculation (lines 99–135)
Pure functions. No side effects.
- `calc_sales_tax(price)` — skill-adjusted sales tax
- `calc_broker_fee_buy(price)` — Jita broker fee
- `calc_broker_fee_sell(price)` — Nullsec structure broker fee (fixed 5%)
- `calc_shipping(jita_price, volume_m3, service)` — ISK/m³ + collateral, with minimum
- `calc_net_profit(...)` — returns full cost breakdown dict

### EVE SSO (lines 141–226)
OAuth2 PKCE-less flow. Opens browser for login, runs local HTTP server on port 12345 to capture callback.
- `sso_login()` — full login flow
- `sso_refresh(tok)` — token refresh
- `get_valid_token()` — load cached token or login/refresh as needed
- Token persisted to `cache/sso_token.json` (**must be gitignored**)

### ESI Helpers (lines 232–270)
- `_esi_pages(path, token, params)` — handles X-Pages pagination automatically
- `_esi_get(path, token, params)` — single non-paginated call

### Bulk Fetchers (lines 276–352)
Performance core. Fetches data in bulk, not per-item.
- `bulk_jita_orders(type_ids)` — loads ALL Forge region orders, filters to Jita 4-4, returns best sell price per item
- `bulk_jita_history(type_ids)` — parallel 30-day avg daily volume for Jita
- `bulk_null_history(type_ids, region_id)` — parallel 30-day avg + activity ratio for nullsec region

### Item Cache (lines 358–394)
Disk-backed JSON cache for item name and packaged volume. Items never change, so this is a permanent cache.
- Stored at `cache/item_info.json`
- Auto-updated when new items are encountered

### Region Resolution (lines 400–415)
- `resolve_region(structure_id, token)` — structure → solar system → constellation → region (3 ESI calls, in-memory cached per run)

### Scanner (lines 421–524)
- `scan_all(access_token)` — main loop over all configured structures
  1. Resolve region
  2. Load structure orders
  3. Bulk-fetch Jita prices + histories
  4. Fetch item infos (cached)
  5. Calculate net profit for each item × exit type (instant-sell vs planned-sell)
  6. Filter by thresholds
  7. Return all deals

### Output (lines 530–578)
- `print_results(deals)` — console: top 20 by profit ISK + per-structure summary
- `print_deal(d)` — detailed breakdown per deal

### Main (lines 584–616)
- Validates CLIENT_ID is configured
- Creates `cache/` directory
- Loads item cache
- Gets valid token
- Runs `scan_all()`
- Prints results
- Saves JSON export: `scan_result_{timestamp}.json`

---

## Important Files

| File | Purpose |
|------|---------|
| `price_scanner.py` | Entire application — entry point and all logic |
| `cache/item_info.json` | Permanent disk cache for EVE item metadata |
| `cache/sso_token.json` | OAuth2 token cache — **must be gitignored, never committed** |
| `scan_result_*.json` | Output files — **should be gitignored** |
| `AGENTS.md` | AI agent workflow rules |
| `PROJECT_STATE.md` | Current status and known issues |
| `TASK_QUEUE.md` | Prioritized task list |
| `SESSION_HANDOFF.md` | Per-session handoff notes |

---

## External APIs

| API | Purpose | Auth |
|-----|---------|------|
| `https://esi.evetech.net/latest` | EVE market data, universe info | Bearer token (SSO) for structure markets; public for region markets |
| `https://login.eveonline.com/v2/oauth/` | EVE SSO OAuth2 | CLIENT_ID + authorization_code flow |

Required ESI scope: `esi-markets.structure_markets.v1`

---

## Performance Characteristics

- Jita bulk fetch: ~5–10 paginated ESI calls (~5–15s)
- Per-structure history: `PARALLEL_WORKERS=10` threads
- Item cache eliminates repeated universe/type calls
- Target runtime: <30s for all configured structures

---

## Known Architectural Gaps

- No retry/backoff for ESI failures
- No rate-limit awareness (ESI has per-second and per-endpoint limits)
- CLIENT_ID hardcoded in source — should move to env var or config file
- No test coverage
- No logging framework — uses `print()` throughout
- Single-file architecture will need splitting if scope grows significantly
