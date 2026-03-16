"""
EVE Nullsec Price Scanner
=========================
Vergleicht Jita-Preise mit Nullsec-Strukturmärkten und zeigt profitable
Import-Deals nach allen Kosten (Shipping, Broker Fee, Sales Tax).

Voraussetzungen:
  pip install requests

Setup:
  1. EVE Developer App anlegen: https://developers.eveonline.com/
     Callback URL: http://localhost:12345/callback
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

load_dotenv()  # lädt .env aus dem Projektordner

# ===========================================================================
# KONFIGURATION – wird aus config.json geladen
# ===========================================================================

CONFIG_FILE      = "config.json"
ITEM_CACHE_FILE  = "cache/item_info.json"
TOKEN_CACHE_FILE = "cache/sso_token.json"

CLIENT_ID = os.getenv("EVE_CLIENT_ID", "")  # .env: EVE_CLIENT_ID=...

def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)

# ===========================================================================
# GEBÜHRENBERECHNUNG
# ===========================================================================

def make_fee_constants(cfg: dict) -> tuple[float, float]:
    """Gibt (sales_tax, broker_sell) zurück, berechnet aus config.json fees."""
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
    shipping    = calc_shipping(jita_price, volume_m3, s)
    fee_sell    = null_price * broker_sell
    tax         = null_price * sales_tax
    total_cost  = jita_price + shipping + fee_sell + tax
    profit_isk  = null_price - total_cost
    profit_pct  = (profit_isk / total_cost * 100) if total_cost > 0 else 0
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
    """Lädt alle Seiten eines paginierten ESI-Endpunkts."""
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
        if r.status_code == 403:
            print(f"  [WARN] Kein Zugriff: {path}")
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
    """Einzelner ESI-Call ohne Paging."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = dict(params or {})
    params.setdefault("datasource", "tranquility")
    r = requests.get(f"{ESI}{path}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

# ===========================================================================
# BULK FETCHER
# ===========================================================================

def bulk_jita_orders(type_ids: set, jita_station_id: int, forge_region_id: int) -> dict:
    """Lädt alle Forge-Orders in ~5–10 Calls, filtert auf Jita 4-4 Sell-Orders.
    Gibt {type_id: best_sell_price} zurück."""
    print("  [BULK] Lade Jita-Orders...")
    t0 = time.time()
    all_orders = _esi_pages(f"/markets/{forge_region_id}/orders/",
                            params={"order_type": "all"})
    print(f"  [BULK] {len(all_orders):,} Orders geladen in {time.time()-t0:.1f}s")
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
    print(f"  [BULK] {len(best_sell):,} Items in Jita 4-4 gefunden")
    return best_sell

def bulk_jita_history(type_ids: set, forge_region_id: int, workers: int) -> dict:
    """30-Tage Durchschnittsvolumen pro Item in Jita (parallel)."""
    print(f"  [BULK] Jita-Historien für {len(type_ids)} Items...")
    t0 = time.time()
    result = {}

    def fetch(tid):
        try:
            hist = _esi_pages(f"/markets/{forge_region_id}/history/",
                              params={"type_id": tid})
            if not hist:
                return tid, 0.0
            recent = sorted(hist, key=lambda x: x["date"], reverse=True)[:30]
            return tid, sum(d["volume"] for d in recent) / len(recent)
        except Exception:
            return tid, 0.0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for tid, vol in ex.map(fetch, type_ids):
            result[tid] = vol

    print(f"  [BULK] Jita-Historien fertig in {time.time()-t0:.1f}s")
    return result

def bulk_null_history(type_ids: set, region_id: int, workers: int) -> dict:
    """30-Tage Durchschnittsvolumen + Aktivitätsquote pro Item in der Nullsec-Region (parallel)."""
    print(f"  [BULK] Nullsec-Historien für {len(type_ids)} Items...")
    t0 = time.time()
    result = {}

    def fetch(tid):
        try:
            hist = _esi_pages(f"/markets/{region_id}/history/",
                              params={"type_id": tid})
            if not hist:
                return tid, 0.0, 0.0
            recent = sorted(hist, key=lambda x: x["date"], reverse=True)[:30]
            active = [d for d in recent if d["volume"] > 0]
            avg   = sum(d["volume"] for d in active) / len(recent) if recent else 0.0
            ratio = len(active) / len(recent) if recent else 0.0
            return tid, avg, ratio
        except Exception:
            return tid, 0.0, 0.0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for tid, avg, ratio in ex.map(fetch, type_ids):
            result[tid] = (avg, ratio)

    print(f"  [BULK] Nullsec-Historien fertig in {time.time()-t0:.1f}s")
    return result

# ===========================================================================
# ITEM-CACHE (Disk)
# ===========================================================================

_item_cache: dict = {}

def load_item_cache():
    global _item_cache
    if os.path.exists(ITEM_CACHE_FILE):
        with open(ITEM_CACHE_FILE) as f:
            _item_cache = json.load(f)
        print(f"  [CACHE] {len(_item_cache):,} Items geladen")

def fetch_item_infos(type_ids: set, workers: int) -> dict:
    """Name + Volumen (m³) pro Item. Unbekannte werden parallel nachgeladen und gecacht."""
    missing = {tid for tid in type_ids if str(tid) not in _item_cache}
    if missing:
        print(f"  [CACHE] {len(missing)} neue Items nachladen...")

        def fetch(tid):
            try:
                info = _esi_get(f"/universe/types/{tid}/")
                return str(tid), {
                    "name":   info.get("name", f"Type {tid}"),
                    "volume": info.get("packaged_volume", info.get("volume", 1.0)),
                }
            except Exception:
                return str(tid), {"name": f"Type {tid}", "volume": 1.0}

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for key, val in ex.map(fetch, missing):
                _item_cache[key] = val

        os.makedirs(os.path.dirname(ITEM_CACHE_FILE), exist_ok=True)
        with open(ITEM_CACHE_FILE, "w") as f:
            json.dump(_item_cache, f)
        print(f"  [CACHE] {len(_item_cache):,} Items gespeichert")

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
        jita_hist  = bulk_jita_history(type_ids, jita["region_id"], workers)
        null_hist  = bulk_null_history(type_ids, s["region"], workers)

        print("  Berechne Deals...")
        deals = []
        for tid in type_ids:
            jita_price        = jita_prices[tid]
            info              = item_infos[tid]
            volume_m3         = info["volume"]
            jita_vol          = jita_hist.get(tid, 0.0)
            null_vol, n_ratio = null_hist.get(tid, (0.0, 0.0))

            if jita_vol < flt["min_daily_vol_jita"]:
                continue
            if null_vol < flt["min_daily_vol_null"]:
                continue

            base = {
                "type_id":    tid,
                "item_name":  info["name"],
                "structure":  struct_name,
                "lane":       s["lane"],
                "volume_m3":  volume_m3,
                "jita_vol":   jita_vol,
                "null_vol":   null_vol,
                "null_ratio": n_ratio,
            }

            # Instant-Sell: direkt an den höchsten Käufer
            if tid in best_buy:
                r = calc_net_profit(jita_price, best_buy[tid], volume_m3, s,
                                    sales_tax, broker_sell)
                if r["profit_isk"] >= flt["min_profit_isk"] and \
                   r["profit_pct"] >= flt["min_profit_pct"]:
                    deals.append({**base, "exit": "instant", **r})

            # Planned-Sell: 0.1% unter dem günstigsten Konkurrenten
            if tid in best_sell:
                target = best_sell[tid] * 0.999
                r = calc_net_profit(jita_price, target, volume_m3, s,
                                    sales_tax, broker_sell)
                if r["profit_isk"] >= flt["min_profit_isk"] and \
                   r["profit_pct"] >= flt["min_profit_pct"]:
                    deals.append({**base, "exit": "planned", **r})

        print(f"  → {len(deals)} profitable Deals  ({time.time()-t0:.1f}s)")
        all_deals.extend(deals)

    print(f"\n[FERTIG] Gesamtzeit: {time.time()-total_start:.1f}s")
    return all_deals

# ===========================================================================
# AUSGABE
# ===========================================================================

def _activity(ratio: float) -> str:
    if ratio >= 0.8: return "aktiv"
    if ratio >= 0.4: return "gelegentlich"
    if ratio >  0:   return "selten"
    return "keine Daten"

def print_deal(d: dict):
    tag = "[INSTANT]" if d["exit"] == "instant" else "[PLANNED]"
    print(f"\n  {tag} {d['item_name']}")
    print(f"    Struktur:      {d['structure']}  (via {d['lane']})")
    print(f"    Jita Kauf:     {d['jita_buy']:>15,.0f} ISK")
    print(f"    Shipping:      {d['shipping']:>15,.0f} ISK  ({d['volume_m3']:.2f} m³)")
    print(f"    Null Verkauf:  {d['null_sell']:>15,.0f} ISK")
    print(f"    Broker + Tax:  {d['fee_sell'] + d['tax']:>15,.0f} ISK")
    print(f"    {'─'*42}")
    print(f"    Nettogewinn:   {d['profit_isk']:>15,.0f} ISK  ({d['profit_pct']:.1f}%)")
    print(f"    Jita Vol:      ~{d['jita_vol']:>8,.0f} / Tag")
    if d["null_vol"] > 0:
        print(f"    Null Vol:      ~{d['null_vol']:>8,.1f} / Tag  "
              f"({_activity(d['null_ratio'])}, {d['null_ratio']*100:.0f}% aktiv / 30 Tage)")

def print_results(deals: list):
    if not deals:
        print("\n[RESULT] Keine profitablen Deals gefunden.")
        print("  Tipp: min_profit_isk / min_profit_pct in config.json reduzieren.")
        return

    top = sorted(deals, key=lambda d: d["profit_isk"], reverse=True)
    print(f"\n{'='*55}")
    print(f"  TOP DEALS – {len(top)} profitable Möglichkeiten")
    print(f"{'='*55}")
    for d in top[:20]:
        print_deal(d)

    print(f"\n{'='*55}")
    print("  ZUSAMMENFASSUNG PRO STRUKTUR")
    print(f"{'='*55}")
    by_s = defaultdict(list)
    for d in deals:
        by_s[d["structure"]].append(d)
    for struct, ds in by_s.items():
        best = max(ds, key=lambda x: x["profit_isk"])
        print(f"  {struct}: {len(ds)} Deals | "
              f"Bester: {best['item_name']} "
              f"+{best['profit_isk']:,.0f} ISK ({best['profit_pct']:.1f}%)")

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

    os.makedirs("cache", exist_ok=True)
    load_item_cache()

    print(f"\n[AUTH] Token prüfen...")
    tok = get_valid_token()
    print(f"[AUTH] OK  |  Sales Tax: {sales_tax*100:.1f}%  |  "
          f"Sell Broker+SCC: {broker_sell*100:.1f}%")

    deals = scan_all(tok["access_token"], cfg, sales_tax, broker_sell)
    print_results(deals)

    if deals:
        out = f"scan_result_{int(time.time())}.json"
        with open(out, "w") as f:
            json.dump(deals, f, indent=2)
        print(f"\n[SAVE] {out}")

if __name__ == "__main__":
    main()
