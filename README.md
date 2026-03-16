# nullsec-trader 2.0

A fast EVE Online market scanner for nullsec traders. Compares Jita prices against your configured nullsec structures and shows you exactly what to buy, where to ship it, and how much profit you'll make – after all fees, taxes and hauling costs.

---

## What it does

Most nullsec trading tools just show you the raw spread between Jita and your structure. This one calculates the **real net profit** after:

- Jita buy price (instant buy against sell orders – no broker fee)
- Hauling costs via your configured logistics service (ITL / HWL)
- Broker Fee + SCC Surcharge + Sales Tax when you sell in null

It also filters out dead markets by checking **regional trade volume** over the last 30 days, so you don't end up buying 50 units of something nobody in your region has touched in a month.

### Two exit strategies

| Type | What it means |
|------|--------------|
| `[INSTANT]` | Someone already has a buy order up in the structure. You ship it and sell immediately. Lower margin, zero wait. |
| `[PLANNED]` | You list your own sell order, undercutting the current cheapest seller by 0.1%. Higher margin, requires patience. |

---

## Performance

Built around bulk fetching instead of per-item API calls:

- All Jita orders loaded in one sweep (~5–10 paginated calls) instead of one call per item
- Nullsec region histories fetched in parallel via `ThreadPoolExecutor`
- Item info (name, volume) permanently cached to disk – never fetched twice

**Result:** Full scan of 4 structures in under 30 seconds instead of 3+ minutes.

---

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`
- An EVE Online account with access to the structures you want to scan

---

## Setup

### 1. Create an EVE Developer App

Go to [https://developers.eveonline.com/](https://developers.eveonline.com/) and create a new application:

- **Connection Type:** Authentication & API Access
- **Callback URL:** `http://localhost:12563/callback`
- **Scopes:** `esi-markets.structure_markets.v1`

Copy your **Client ID**.

### 2. Create a `.env` file

```
EVE_CLIENT_ID=your_client_id_here
```

### 3. Configure structures and rates

Open `config.json` and adjust the structures and shipping rates to match your setup:

```json
"structures": {
    "My Structure": {
        "id":         1040804972352,
        "region":     10000059,
        "lane":       "HWL",
        "per_m3":     1250,
        "collateral": 0.01,
        "min_cost":   5000000,
        "max_m3":     350000
    }
}
```

**Fields:**

| Field | Description |
|-------|-------------|
| `id` | Structure ID (find it via Show Info in-game) |
| `region` | ESI region ID of the structure's region |
| `lane` | Logistics service label (free text, shown in output) |
| `per_m3` | Hauling cost in ISK per m³ |
| `collateral` | Collateral rate as a decimal (0.01 = 1%, 0.0 = none) |
| `min_cost` | Minimum contract cost in ISK |
| `max_m3` | Max volume per contract in m³ |

### 4. Set your trade skills and filters

Also in `config.json`:

```json
"fees": {
    "sales_tax_base":            0.075,
    "sales_tax_per_accounting":  0.005,
    "accounting_level":          3,
    "sell_broker_fee":           0.030,
    "scc_surcharge":             0.005
},
"filters": {
    "min_profit_isk":     1000000,
    "min_profit_pct":     10.0,
    "min_daily_vol_jita": 5,
    "min_daily_vol_null": 1
}
```

---

## Running

```bash
pip install -r requirements.txt
python price_scanner.py
```

On first run the browser will open for EVE SSO login. After that the token is cached in `cache/sso_token.json` and refreshed automatically.

---

## Output

```
[INSTANT] Caldari Navy Mjolnir Heavy Missile
    Struktur:      O4T  (via HWL)
    Jita Kauf:           4,200,000 ISK
    Shipping:            5,000,000 ISK  (0.05 m³)
    Null Verkauf:       12,500,000 ISK
    Broker + Tax:          412,500 ISK
    ──────────────────────────────────────────
    Nettogewinn:         2,887,500 ISK  (29.9%)
    Jita Vol:               ~8,400 / Tag
    Null Vol:                 ~14.2 / Tag  (aktiv, 87% aktiv / 30 Tage)
```

The activity indicator shows how consistently the item trades in your region:

| Label | Meaning |
|-------|---------|
| `aktiv` | Traded on 80%+ of the last 30 days |
| `gelegentlich` | Traded on 40–80% of days |
| `selten` | Traded occasionally – proceed with caution |
| `keine Daten` | No regional history available |

Results are also saved as `scan_result_<timestamp>.json` for further analysis.

---

## Adjusting filters

In `config.json` under `filters`:

| Key | Effect |
|-----|--------|
| `min_profit_isk` | Minimum net profit per deal in ISK |
| `min_profit_pct` | Minimum profit as a percentage of total cost |
| `min_daily_vol_jita` | Minimum average daily volume in Jita (liquidity check) |
| `min_daily_vol_null` | Minimum average daily volume in the null region (dead market filter) |

Raise these to get fewer, higher-quality deals. Lower them if you're getting no results.

---

## Cache files

| File | What it is |
|------|-----------|
| `cache/sso_token.json` | EVE SSO token – do not share or commit |
| `cache/item_info.json` | Item names and volumes – safe to delete, will be rebuilt |

Both are excluded from version control via `.gitignore`.

---

## Notes

- The regional volume check covers the **entire region**, not just your specific structure. It's the best proxy available since ESI doesn't expose per-structure trade history.
- This tool is read-only. It never places orders on your behalf.
