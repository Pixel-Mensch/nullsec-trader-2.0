"""
EVE Nullsec Price Scanner
=========================
Vergleicht Jita-Preise mit Nullsec-Strukturmärkten und zeigt profitable
Import-Deals nach allen Kosten (Shipping, Broker Fee, Sales Tax).

Voraussetzungen:
  pip install -r requirements.txt

Setup:
  1. EVE Developer App anlegen: https://developers.eveonline.com/
     Callback URL: http://localhost:12563/callback
     Scope:        esi-markets.structure_markets.v1
  2. .env anlegen:  EVE_CLIENT_ID=deine_client_id
  3. Strukturen + Raten in config.json anpassen
  4. python price_scanner.py
"""

import json
import os
import threading
import time
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

# ===========================================================================
# KONFIGURATION – wird aus config.json geladen
# ===========================================================================

CONFIG_FILE      = "config.json"
ITEM_CACHE_FILE  = "cache/item_info.json"
TOKEN_CACHE_FILE = "cache/sso_token.json"
HIST_CACHE_FILE  = "cache/history.json"
HIST_CACHE_TTL   = 23 * 3600  # Historien-Cache gilt 23 Stunden (EVE aktualisiert täglich)

CLIENT_ID = os.getenv("EVE_CLIENT_ID", "")

def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)

# ===========================================================================
# GEBÜHRENBERECHNUNG
# ===========================================================================

def make_fee_constants(cfg: dict) -> tuple[float, float]:
    f = cfg["fees"]
    sales_tax   = f["sales_tax_base"] - f["sales_tax_per_accounting"] * f["accounting_level"]
    broker_sell = f["sell_broker_fee"] + f["scc_surcharge"]
    return sales_tax, broker_sell

def calc_shipping(jita_price: float, volume_m3: float, s: dict) -> float:
    raw = s["per_m3"] * volume_m3 + s["collateral"] * jita_price
    return max(raw, s["min_cost"])

def calc_net_profit(jita_price: float, null_price: float,
                    volume_m3: float, s: dict,
                    sales_tax: float, broker_sell: float) -> dict:
    shipping   = calc_shipping(jita_price, volume_m3, s)
    fee_sell   = null_price * broker_sell
    tax        = null_price * sales_tax
    total_cost = jita_price + shipping + fee_sell + tax
    profit_isk = null_price - total_cost
    profit_pct = (profit_isk / total_cost * 100) if total_cost > 0 else 0
    return {
        "jita_buy":   jita_price,
        "shipping":   shipping,
        "fee_sell":   fee_sell,
        "tax":        tax,
        "total_cost": total_cost,
        "null_sell":  null_price,
        "profit_isk": profit_isk,
        "profit_pct": profit_pct,
    }

# ===========================================================================
# EVE SSO
# ===========================================================================

SSO_AUTH_URL  = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
CALLBACK_PORT = 12563
CALLBACK_URL  = f"http://localhost:{CALLBACK_PORT}/callback"
SCOPE         = "esi-markets.structure_markets.v1"

_auth_code = None

class _CBHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code
        p = parse_qs(urlparse(self.path).query)
        if "code" in p:
            _auth_code = p["code"][0]
            self.send_response(200); self.end_headers()
            self.wfile.write(b"Login erfolgreich! Fenster schliessen.")
        else:
            self.send_response(400); self.end_headers()
    def log_message(self, *_): pass

def _cb_server():
    s = HTTPServer(("localhost", CALLBACK_PORT), _CBHandler)
    s.timeout = 120
    s.handle_request()

def sso_login() -> dict:
    global _auth_code
    _auth_code = None
    url = (f"{SSO_AUTH_URL}?"
           + urlencode({"response_type": "code", "client_id": CLIENT_ID,
                        "redirect_uri": CALLBACK_URL, "scope": SCOPE,
                        "state": "scanner"}))
    print("[SSO] Browser wird geöffnet...")
    threading.Thread(target=_cb_server, daemon=True).start()
    webbrowser.open(url)
    for _ in range(120):
        time.sleep(1)
        if _auth_code:
            break
    else:
        raise TimeoutError("Login Timeout (120s)")
    r = requests.post(SSO_TOKEN_URL, data={
        "grant_type": "authorization_code", "code": _auth_code,
        "client_id": CLIENT_ID, "redirect_uri": CALLBACK_URL,
    })
    r.raise_for_status()
    tok = r.json()
    tok["expires_at"] = time.time() + tok.get("expires_in", 1200) - 60
    _save_token(tok)
    print("[SSO] Login erfolgreich!")
    return tok

def sso_refresh(tok: dict) -> dict:
    r = requests.post(SSO_TOKEN_URL, data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": CLIENT_ID,
    })
    r.raise_for_status()
    new = r.json()
    new["expires_at"] = time.time() + new.get("expires_in", 1200) - 60
    new.setdefault("refresh_token", tok["refresh_token"])
    _save_token(new)
    return new

def get_valid_token() -> dict:
    tok = _load_token()
    if tok is None:
        return sso_login()
    if time.time() > tok.get("expires_at", 0):
        print("[SSO] Token wird erneuert...")
        try:
            return sso_refresh(tok)
        except Exception:
            return sso_login()
    return tok

def _load_token():
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE) as f:
            return json.load(f)
    return None

def _save_token(tok):
    os.makedirs(os.path.dirname(TOKEN_CACHE_FILE), exist_ok=True)
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump(tok, f, indent=2)

# ===========================================================================
# ESI – HILFSFUNKTIONEN
# ===========================================================================

ESI = "https://esi.evetech.net/latest"

def _esi_pages(path: str, token: str = None, params: dict = None) -> list:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = dict(params or {})
    params.setdefault("datasource", "tranquility")
    results = []
    page = 1
    while True:
        params["page"] = page
        r = requests.get(f"{ESI}{path}", headers=headers, params=params, timeout=30)
        if r.status_code in (401, 403):
            print(f"  [WARN] Kein Zugriff ({r.status_code}): {path}")
            return []
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return data
        results.extend(data)
        if page >= int(r.headers.get("X-Pages", 1)):
            break
        page += 1
    return results

def _esi_get(path: str, token: str = None, params: dict = None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = dict(params or {})
    params.setdefault("datasource", "tranquility")
    r = requests.get(f"{ESI}{path}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

# ===========================================================================
# HISTORIEN-CACHE (Disk, 23h TTL)
# ===========================================================================

def _load_hist_cache() -> dict:
    if os.path.exists(HIST_CACHE_FILE):
        with open(HIST_CACHE_FILE) as f:
            return json.load(f)
    return {}

def _save_hist_cache(data: dict):
    os.makedirs(os.path.dirname(HIST_CACHE_FILE), exist_ok=True)
    with open(HIST_CACHE_FILE, "w") as f:
        json.dump(data, f)

def _hist_key(region_id: int, type_id: int) -> str:
    return f"{region_id}:{type_id}"

# ===========================================================================
# BULK FETCHER
# ===========================================================================

def _progress(label: str, done: int, total: int, t0: float):
    pct      = done / total * 100
    elapsed  = time.time() - t0
    eta      = elapsed / done * (total - done) if done else 0
    bar_len  = 20
    filled   = int(bar_len * done / total)
    bar      = "█" * filled + "░" * (bar_len - filled)
    print(f"\r  {label}  [{bar}] {done}/{total} ({pct:.0f}%)  ETA {eta:.0f}s  ",
          end="", flush=True)

def bulk_jita_orders(type_ids: set, jita_station_id: int, forge_region_id: int) -> dict:
    t0      = time.time()
    headers = {"Accept": "application/json", "datasource": "tranquility"}
    params  = {"order_type": "all", "datasource": "tranquility"}

    # Erste Seite laden um Gesamtseitenanzahl zu kennen
    params["page"] = 1
    r = requests.get(f"{ESI}/markets/{forge_region_id}/orders/",
                     headers=headers, params=params, timeout=30)
    r.raise_for_status()
    all_orders   = r.json()
    total_pages  = int(r.headers.get("X-Pages", 1))
    print(f"\r  Jita-Orders: Seite 1/{total_pages}  ({len(all_orders):,} Orders)  ",
          end="", flush=True)

    for page in range(2, total_pages + 1):
        params["page"] = page
        r = requests.get(f"{ESI}/markets/{forge_region_id}/orders/",
                         headers=headers, params=params, timeout=30)
        r.raise_for_status()
        all_orders.extend(r.json())
        print(f"\r  Jita-Orders: Seite {page}/{total_pages}  ({len(all_orders):,} Orders)  ",
              end="", flush=True)

    best_sell = {}
    for o in all_orders:
        if o.get("location_id") != jita_station_id:
            continue
        if o["type_id"] not in type_ids:
            continue
        if not o["is_buy_order"]:
            tid = o["type_id"]
            if tid not in best_sell or o["price"] < best_sell[tid]:
                best_sell[tid] = o["price"]
    print(f"\r  Jita-Orders: {len(all_orders):,} Orders → "
          f"{len(best_sell):,} Items in Jita 4-4  ({time.time()-t0:.1f}s)  ")
    return best_sell

def bulk_history(type_ids: set, region_id: int, workers: int,
                 label: str, hist_cache: dict) -> dict:
    """Lädt 30-Tage-Historien. Gecachte Einträge (< 23h) werden übersprungen.
    Gibt {tid: (avg_vol, ratio, avg_price, avg_orders)} zurück."""
    now      = time.time()
    result   = {}
    to_fetch = []

    for tid in type_ids:
        key   = _hist_key(region_id, tid)
        entry = hist_cache.get(key)
        if entry and now - entry["ts"] < HIST_CACHE_TTL:
            result[tid] = (entry["avg"], entry["ratio"],
                           entry.get("avg_price", 0.0), entry.get("avg_orders", 0.0))
        else:
            to_fetch.append(tid)

    cached_count = len(type_ids) - len(to_fetch)
    if cached_count:
        print(f"  {label}: {cached_count}/{len(type_ids)} aus Cache", flush=True)

    if not to_fetch:
        return result

    t0      = time.time()
    counter = [0]
    lock    = threading.Lock()

    def fetch(tid):
        try:
            hist = _esi_pages(f"/markets/{region_id}/history/",
                              params={"type_id": tid})
            if not hist:
                return tid, 0.0, 0.0, 0.0, 0.0
            recent     = sorted(hist, key=lambda x: x["date"], reverse=True)[:30]
            active     = [d for d in recent if d["volume"] > 0]
            avg_vol    = sum(d["volume"] for d in active) / len(recent) if recent else 0.0
            ratio      = len(active) / len(recent) if recent else 0.0
            # Gewichteter Durchschnittspreis (nach Volumen)
            total_vol  = sum(d["volume"] for d in active)
            avg_price  = (sum(d["average"] * d["volume"] for d in active) / total_vol
                          if total_vol > 0 else 0.0)
            avg_orders = sum(d["order_count"] for d in recent) / len(recent) if recent else 0.0
            return tid, avg_vol, ratio, avg_price, avg_orders
        except Exception:
            return tid, 0.0, 0.0, 0.0, 0.0
        finally:
            with lock:
                counter[0] += 1
                _progress(label, counter[0], len(to_fetch), t0)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for tid, avg_vol, ratio, avg_price, avg_orders in ex.map(fetch, to_fetch):
            result[tid] = (avg_vol, ratio, avg_price, avg_orders)
            key = _hist_key(region_id, tid)
            hist_cache[key] = {"avg": avg_vol, "ratio": ratio,
                               "avg_price": avg_price, "avg_orders": avg_orders,
                               "ts": now}

    print(f"\n  {label} fertig  ({time.time()-t0:.1f}s)", flush=True)
    return result

# ===========================================================================
# ITEM-CACHE (Disk, permanent)
# ===========================================================================

_item_cache: dict = {}

def load_item_cache():
    global _item_cache
    if os.path.exists(ITEM_CACHE_FILE):
        with open(ITEM_CACHE_FILE) as f:
            _item_cache = json.load(f)
        print(f"  Item-Cache: {len(_item_cache):,} Items")

def fetch_item_infos(type_ids: set, workers: int) -> dict:
    missing = {tid for tid in type_ids if str(tid) not in _item_cache}
    if missing:
        print(f"  {len(missing)} neue Items nachladen...", flush=True)
        t0      = time.time()
        counter = [0]
        lock    = threading.Lock()

        def fetch(tid):
            try:
                info = _esi_get(f"/universe/types/{tid}/")
                return str(tid), {
                    "name":   info.get("name", f"Type {tid}"),
                    "volume": info.get("packaged_volume", info.get("volume", 1.0)),
                }
            except Exception:
                return str(tid), {"name": f"Type {tid}", "volume": 1.0}
            finally:
                with lock:
                    counter[0] += 1
                    _progress("Items", counter[0], len(missing), t0)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for key, val in ex.map(fetch, missing):
                _item_cache[key] = val

        print()
        os.makedirs(os.path.dirname(ITEM_CACHE_FILE), exist_ok=True)
        with open(ITEM_CACHE_FILE, "w") as f:
            json.dump(_item_cache, f)

    return {tid: _item_cache.get(str(tid), {"name": f"Type {tid}", "volume": 1.0})
            for tid in type_ids}

# ===========================================================================
# SCANNER
# ===========================================================================

def scan_all(access_token: str, cfg: dict,
             sales_tax: float, broker_sell: float) -> list:
    structures = cfg["structures"]
    jita       = cfg["jita"]
    flt        = cfg["filters"]
    workers    = cfg["performance"]["parallel_workers"]

    hist_cache  = _load_hist_cache()
    total_start = time.time()
    all_deals   = []

    for struct_name, s in structures.items():
        print(f"\n{'='*55}")
        print(f"[SCAN] {struct_name}  (via {s['lane']})")
        print(f"{'='*55}")
        t0 = time.time()

        struct_orders = _esi_pages(
            f"/markets/structures/{s['id']}/", token=access_token)
        if not struct_orders:
            print("  Keine Orders oder kein Zugriff.")
            continue

        best_buy  = {}
        best_sell = {}
        for o in struct_orders:
            tid = o["type_id"]
            if o["is_buy_order"]:
                if tid not in best_buy or o["price"] > best_buy[tid]:
                    best_buy[tid] = o["price"]
            else:
                if tid not in best_sell or o["price"] < best_sell[tid]:
                    best_sell[tid] = o["price"]

        type_ids = set(best_buy) | set(best_sell)
        print(f"  {len(type_ids):,} Items  |  "
              f"{len(best_buy):,} Kaufaufträge  |  "
              f"{len(best_sell):,} Verkaufsaufträge")

        jita_prices = bulk_jita_orders(type_ids, jita["station_id"], jita["region_id"])
        type_ids    = type_ids & set(jita_prices)
        if not type_ids:
            print("  Kein Overlap mit Jita-Sortiment.")
            continue

        item_infos = fetch_item_infos(type_ids, workers)
        jita_hist  = bulk_history(type_ids, jita["region_id"], workers,
                                  "Jita-Historien", hist_cache)
        null_hist  = bulk_history(type_ids, s["region"], workers,
                                  f"Null-Historien ({struct_name})", hist_cache)

        deals = []
        for tid in type_ids:
            jita_price                       = jita_prices[tid]
            info                             = item_infos[tid]
            volume_m3                        = info["volume"]
            jita_vol                         = jita_hist.get(tid, (0,0,0,0))[0]
            null_vol, n_ratio, avg_p, avg_ord = null_hist.get(tid, (0.0, 0.0, 0.0, 0.0))

            if jita_vol < flt["min_daily_vol_jita"]:
                continue
            if null_vol < flt["min_daily_vol_null"]:
                continue

            base = {
                "type_id":       tid,
                "item_name":     info["name"],
                "structure":     struct_name,
                "lane":          s["lane"],
                "volume_m3":     volume_m3,
                "jita_vol":      jita_vol,
                "null_vol":      null_vol,
                "null_ratio":    n_ratio,
                "avg_null_price": avg_p,
                "avg_orders":    avg_ord,
            }

            # INSTANT: sofort an höchsten Käufer
            if tid in best_buy:
                r = calc_net_profit(jita_price, best_buy[tid], volume_m3, s,
                                    sales_tax, broker_sell)
                if r["profit_isk"] >= flt["min_profit_isk"] and \
                   r["profit_pct"] >= flt["min_profit_pct"]:
                    deals.append({**base, "exit": "instant", **r})

            # PLANNED: Preis gegen historischen Schnitt validieren
            if tid in best_sell and avg_p > 0:
                max_list = avg_p * flt.get("max_price_vs_avg", 1.5)
                if best_sell[tid] > max_list:
                    continue  # Listing weit über Schnitt → vermutlich Manipulation
                target = min(best_sell[tid] * 0.999, avg_p * 1.05)
                r = calc_net_profit(jita_price, target, volume_m3, s,
                                    sales_tax, broker_sell)
                if r["profit_isk"] >= flt["min_profit_isk"] and \
                   r["profit_pct"] >= flt["min_profit_pct"]:
                    r["price_vs_avg"] = target / avg_p
                    deals.append({**base, "exit": "planned", **r})
            elif tid in best_sell and avg_p == 0:
                # Keine Preishistorie → konservativ: deal zulassen aber ohne Preis-Cap
                target = best_sell[tid] * 0.999
                r = calc_net_profit(jita_price, target, volume_m3, s,
                                    sales_tax, broker_sell)
                if r["profit_isk"] >= flt["min_profit_isk"] and \
                   r["profit_pct"] >= flt["min_profit_pct"]:
                    r["price_vs_avg"] = None
                    deals.append({**base, "exit": "planned", **r})

        print(f"  → {len(deals)} Deals  ({time.time()-t0:.1f}s)")
        all_deals.extend(deals)

    _save_hist_cache(hist_cache)
    print(f"\n[FERTIG] Gesamtzeit: {time.time()-total_start:.1f}s")
    return all_deals

# ===========================================================================
# BUDGET-PLANUNG
# ===========================================================================

def parse_budget(s: str) -> int:
    """Parst '500m', '1b', '500000000' → int ISK."""
    s = s.strip().lower().replace(".", "").replace(",", "").replace("_", "")
    if s.endswith("b"):
        return int(float(s[:-1]) * 1_000_000_000)
    if s.endswith("m"):
        return int(float(s[:-1]) * 1_000_000)
    if s.endswith("k"):
        return int(float(s[:-1]) * 1_000)
    return int(s)

def _plan_units(deal: dict, remaining: float, s: dict, max_days: int = 45) -> int:
    """Berechnet wie viele Einheiten sinnvoll sind (Budget, Volumen, Markttiefe)."""
    jita_per = deal["jita_buy"]
    vol_per  = deal["volume_m3"]
    if jita_per <= 0 or vol_per <= 0:
        return 0
    by_budget = int(remaining / jita_per)
    by_m3     = int(s["max_m3"] / vol_per)
    by_jita   = max(1, int(deal["jita_vol"] * 5))
    # Null-Volumen: max so viele Einheiten wie sich in max_days verkaufen
    by_null   = max(1, int(deal["null_vol"] * max_days)) if deal["null_vol"] > 0 else by_jita
    return max(0, min(by_budget, by_m3, by_jita, by_null))

def _calc_batch(deal: dict, units: int, s: dict,
                sales_tax: float, broker_sell: float) -> dict:
    """Neu-Berechnung von Shipping + Profit für eine bestimmte Menge."""
    jita_total = deal["jita_buy"]  * units
    vol_total  = deal["volume_m3"] * units
    null_total = deal["null_sell"] * units
    shipping   = calc_shipping(jita_total, vol_total, s)
    fee        = null_total * broker_sell
    tax        = null_total * sales_tax
    total_cost = jita_total + shipping + fee + tax
    profit     = null_total - total_cost
    profit_pct = (profit / total_cost * 100) if total_cost > 0 else 0
    return {
        "units":       units,
        "jita_total":  jita_total,
        "vol_total":   vol_total,
        "null_total":  null_total,
        "shipping":    shipping,
        "fee_tax":     fee + tax,
        "total_cost":  total_cost,
        "profit":      profit,
        "profit_pct":  profit_pct,
    }

def plan_structure(deals: list, budget: float, s: dict,
                   sales_tax: float, broker_sell: float,
                   max_days: int = 45) -> list:
    """Greedy-Allokation: beste ROI-Deals zuerst, bis Budget aufgebraucht."""
    remaining = budget
    plan      = []

    # Bestes Deal pro Item (instant bevorzugt, sonst planned)
    best_per_item: dict = {}
    for d in deals:
        tid = d["type_id"]
        if tid not in best_per_item:
            best_per_item[tid] = d
        else:
            cur = best_per_item[tid]
            if d["exit"] == "instant" and cur["exit"] != "instant":
                best_per_item[tid] = d
            elif d["profit_pct"] > cur["profit_pct"] and d["exit"] == cur["exit"]:
                best_per_item[tid] = d

    sorted_deals = sorted(best_per_item.values(),
                          key=lambda d: d["profit_pct"], reverse=True)

    for d in sorted_deals:
        if remaining < d["jita_buy"]:
            continue
        units = _plan_units(d, remaining, s, max_days)
        if units <= 0:
            continue
        batch = _calc_batch(d, units, s, sales_tax, broker_sell)
        if batch["profit"] <= 0:
            continue
        plan.append({**d, "batch": batch})
        remaining -= batch["jita_total"]
        if remaining < 1_000_000:
            break

    return plan

# ===========================================================================
# AUSGABE
# ===========================================================================

def _activity(ratio: float) -> str:
    if ratio >= 0.8: return "aktiv"
    if ratio >= 0.4: return "gelegentlich"
    if ratio >  0:   return "selten"
    return "?"

def _isk(n: float) -> str:
    if abs(n) >= 1_000_000_000: return f"{n/1_000_000_000:.2f}b"
    if abs(n) >= 1_000_000:     return f"{n/1_000_000:.1f}m"
    return f"{n:,.0f}"

def print_results(deals: list):
    if not deals:
        print("\n[RESULT] Keine profitablen Deals gefunden.")
        print("  Tipp: min_profit_isk / min_profit_pct in config.json reduzieren.")
        return

    top = sorted(deals, key=lambda d: d["profit_isk"], reverse=True)
    W   = 36

    print(f"\n{'='*74}")
    print(f"  {'#':<3}  {'Item':<{W}}  {'Struktur':<18}  {'Exit':<7}  "
          f"{'Gewinn':>10}  {'%':>5}")
    print(f"  {'─'*3}  {'─'*W}  {'─'*18}  {'─'*7}  {'─'*10}  {'─'*5}")
    for i, d in enumerate(top[:30], 1):
        tag = "INSTANT" if d["exit"] == "instant" else "PLANNED"
        print(f"  {i:<3}  {d['item_name'][:W]:<{W}}  {d['structure']:<18}  "
              f"{tag:<7}  {_isk(d['profit_isk']):>10}  {d['profit_pct']:>4.1f}%")

    print(f"\n{'='*74}")
    print("  TOP 5 – DETAIL")
    print(f"{'='*74}")
    for d in top[:5]:
        tag = "INSTANT" if d["exit"] == "instant" else "PLANNED"
        print(f"\n  [{tag}] {d['item_name']}")
        print(f"    Struktur:     {d['structure']}  (via {d['lane']})")
        print(f"    Jita Kauf:    {d['jita_buy']:>15,.0f} ISK  ({d['volume_m3']:.2f} m³/Stk)")
        print(f"    Null Verkauf: {d['null_sell']:>15,.0f} ISK")
        print(f"    Shipping:     {d['shipping']:>15,.0f} ISK")
        print(f"    Broker+Tax:   {d['fee_sell']+d['tax']:>15,.0f} ISK")
        print(f"    {'─'*40}")
        print(f"    Gewinn/Stk:   {d['profit_isk']:>15,.0f} ISK  ({d['profit_pct']:.1f}%)")
        print(f"    Jita Vol:     ~{d['jita_vol']:>7,.0f}/Tag   "
              f"Null: ~{d['null_vol']:>5.1f}/Tag  ({_activity(d['null_ratio'])})")
        if d["exit"] == "planned" and d.get("avg_null_price", 0) > 0:
            pva = d.get("price_vs_avg")
            ref = d["avg_null_price"]
            flag = "✓" if pva and pva <= 1.2 else ("⚠" if pva and pva <= 1.5 else "?")
            print(f"    Referenzpreis:{ref:>15,.0f} ISK  "
                  f"Listing = {(pva or 0)*100:.0f}% vom Schnitt  {flag}")

    print(f"\n{'='*74}")
    print("  ZUSAMMENFASSUNG PRO STRUKTUR")
    print(f"{'─'*74}")
    by_s = defaultdict(list)
    for d in deals:
        by_s[d["structure"]].append(d)
    for struct, ds in sorted(by_s.items()):
        best = max(ds, key=lambda x: x["profit_isk"])
        print(f"  {struct:<22}  {len(ds):>3} Deals  |  "
              f"Bester: {best['item_name'][:28]}  "
              f"+{_isk(best['profit_isk'])} ({best['profit_pct']:.1f}%)")

def print_and_save_plan(plans: dict, budget: int, cfg: dict, path: str):
    """Gibt den Budget-Plan aus und speichert ihn als .txt."""
    ts    = time.strftime("%Y-%m-%d %H:%M")
    lines = []

    def w(s=""):
        print(s)
        lines.append(s)

    w(f"EVE Nullsec Price Scanner – Trade Plan")
    w(f"Erstellt: {ts}    Budget pro Struktur: {_isk(budget)} ISK")
    w("=" * 74)

    total_invest  = 0
    total_profit  = 0

    for struct_name, plan in plans.items():
        s = cfg["structures"][struct_name]
        w()
        w(f"STRUKTUR: {struct_name}  (via {s['lane']})")
        w("─" * 74)

        if not plan:
            w("  Keine profitable Allokation möglich.")
            continue

        invest = sum(p["batch"]["jita_total"] for p in plan)
        profit = sum(p["batch"]["profit"]     for p in plan)
        vol    = sum(p["batch"]["vol_total"]  for p in plan)
        ship   = sum(p["batch"]["shipping"]   for p in plan)
        pct    = profit / invest * 100 if invest else 0

        w(f"  Budget eingesetzt:  {invest:>15,.0f} ISK  ({invest/budget*100:.0f}% des Budgets)")
        w(f"  Gesamtshipping:     {ship:>15,.0f} ISK")
        w(f"  Erwarteter Gewinn:  {profit:>15,.0f} ISK  ({pct:.1f}%)")
        w(f"  Gesamtvolumen:      {vol:>15,.0f} m³")
        w()

        NS = 6
        NI = max(len(p["item_name"]) for p in plan) if plan else 30
        w(f"  {'Item':<{NI}}  {'Menge':>{NS}}  {'Jita/Stk':>12}  "
          f"{'Invest':>14}  {'Null/Stk':>12}  {'Gewinn':>12}  {'%':>5}  {'~Tage':>5}")
        w(f"  {'─'*NI}  {'─'*NS}  {'─'*12}  {'─'*14}  {'─'*12}  {'─'*12}  {'─'*5}  {'─'*5}")

        for p in sorted(plan, key=lambda x: x["batch"]["profit"], reverse=True):
            b        = p["batch"]
            tag      = "I" if p["exit"] == "instant" else "P"
            days_est = (b["units"] / p["null_vol"]) if p["null_vol"] > 0 else 0
            w(f"  {p['item_name']:<{NI}}  {b['units']:>{NS},}  "
              f"{p['jita_buy']:>12,.0f}  {b['jita_total']:>14,.0f}  "
              f"{p['null_sell']:>12,.0f}  {b['profit']:>12,.0f}  "
              f"{b['profit_pct']:>4.1f}%  {days_est:>4.0f}d  [{tag}]")

        total_invest += invest
        total_profit += profit

    w()
    w("=" * 74)
    w(f"  GESAMT")
    w(f"  Eingesetzt:  {total_invest:>15,.0f} ISK")
    w(f"  Gewinn:      {total_profit:>15,.0f} ISK  "
      f"({total_profit/total_invest*100:.1f}%)" if total_invest else "")
    w(f"  Gespeichert: {path}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

# ===========================================================================
# MAIN
# ===========================================================================

def main():
    print("=" * 55)
    print("  EVE Nullsec Price Scanner")
    print("=" * 55)

    if not CLIENT_ID:
        print("\n[FEHLER] EVE_CLIENT_ID fehlt!")
        print("  1. https://developers.eveonline.com/ → neue App anlegen")
        print(f"  2. Callback URL: {CALLBACK_URL}")
        print(f"  3. Scope: {SCOPE}")
        print("  4. .env anlegen: EVE_CLIENT_ID=deine_client_id")
        return

    cfg = load_config()
    sales_tax, broker_sell = make_fee_constants(cfg)

    # Budget abfragen
    print()
    raw = input("Budget pro Struktur (z.B. 500m, 1b, 0 = kein Plan): ").strip()
    budget = 0
    if raw and raw != "0":
        try:
            budget = parse_budget(raw)
            print(f"  → {budget:,.0f} ISK pro Struktur")
        except ValueError:
            print("  Ungültige Eingabe, kein Budget-Plan.")

    os.makedirs("cache", exist_ok=True)
    load_item_cache()

    print(f"\n[AUTH] Token prüfen...")
    tok = get_valid_token()
    print(f"[AUTH] OK  |  Sales Tax: {sales_tax*100:.1f}%  |  "
          f"Sell Broker+SCC: {broker_sell*100:.1f}%")

    deals = scan_all(tok["access_token"], cfg, sales_tax, broker_sell)

    if not deals:
        return

    ts = int(time.time())

    # JSON speichern
    json_path = f"scan_result_{ts}.json"
    with open(json_path, "w") as f:
        json.dump(deals, f, indent=2)

    # Deals-Übersicht
    print_results(deals)

    # Budget-Plan
    if budget > 0:
        by_struct = defaultdict(list)
        for d in deals:
            by_struct[d["structure"]].append(d)

        plans = {}
        for struct_name, s in cfg["structures"].items():
            struct_deals = by_struct.get(struct_name, [])
            max_days = cfg["filters"].get("max_days_to_sell", 45)
            plans[struct_name] = plan_structure(
                struct_deals, budget, s, sales_tax, broker_sell, max_days)

        txt_path = f"trade_plan_{ts}.txt"
        print(f"\n{'='*74}")
        print(f"  BUDGET-PLAN  ({_isk(budget)} ISK pro Struktur)")
        print(f"{'='*74}")
        print_and_save_plan(plans, budget, cfg, txt_path)

if __name__ == "__main__":
    main()
