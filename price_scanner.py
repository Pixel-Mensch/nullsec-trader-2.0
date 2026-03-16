"""
EVE Online Nullsec Price Scanner  (Performance Edition)
========================================================
Vergleicht Jita-Preise mit konfigurierten Nullsec-Strukturen.

Performance-Ansatz:
  - Jita + Nullsec-Region werden KOMPLETT in einem Rutsch geladen (Bulk-Fetch)
  - Item-Infos werden dauerhaft auf Disk gecacht (ändern sich nie)
  - Nullsec-Regionshistorien laufen PARALLEL via ThreadPoolExecutor
  - Kein per-Item ESI-Call mehr → von ~3 Minuten auf <30 Sekunden

Voraussetzungen:
  pip install requests

Setup:
  1. EVE Developer App: https://developers.eveonline.com/
     - Callback URL: http://localhost:12345/callback
     - Scope: esi-markets.structure_markets.v1
  2. CLIENT_ID unten eintragen
  3. Struktur-IDs eintragen
  4. python price_scanner.py
"""

import json
import os
import threading
import time
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

# ===========================================================================
# KONFIGURATION – hier alles anpassen
# ===========================================================================

CLIENT_ID = "DEINE_CLIENT_ID_HIER"  # Von https://developers.eveonline.com/

# Struktur-IDs deiner Nullsec-Märkte
STRUCTURES = {
    "O4T (HWL Ziel)":  1234567890123,  # <-- echte ID eintragen
    "UALX-3":          1234567890124,
    "C-J6 (ITL Ziel)": 1234567890125,
    "R-A":             1234567890126,
}

# Jita 4-4 NPC Station ID + Region
JITA_STATION_ID     = 60003760
THE_FORGE_REGION_ID = 10000002

# Shipping-Kosten
SHIPPING = {
    "HWL": {
        "isk_per_m3":     500,        # ISK pro m³ – anpassen!
        "collateral_pct": 0.006,      # 0.6% Collateral – anpassen!
        "min_cost":       5_000_000,  # Mindestpreis pro Lieferung
        "max_m3":         860_000,
    },
    "ITL": {
        "isk_per_m3":     500,
        "collateral_pct": 0.006,
        "min_cost":       5_000_000,
        "max_m3":         860_000,
    },
}

STRUCTURE_SHIPPING = {
    "O4T (HWL Ziel)":  "HWL",
    "UALX-3":          "HWL",
    "C-J6 (ITL Ziel)": "ITL",
    "R-A":             "ITL",
}

# Skill-Levels
SKILLS = {
    "accounting":                3,
    "broker_relations":          3,
    "advanced_broker_relations": 0,
}

# Filter
MIN_PROFIT_ISK     = 1_000_000
MIN_PROFIT_PCT     = 10.0
MIN_DAILY_VOL_JITA = 5
MIN_DAILY_VOL_NULL = 1

# Performance
PARALLEL_WORKERS = 10   # Parallele Threads für History-Calls
ITEM_CACHE_FILE  = "cache/item_info.json"
TOKEN_CACHE_FILE = "cache/sso_token.json"

# ===========================================================================
# GEBÜHRENBERECHNUNG
# ===========================================================================

def calc_sales_tax(price: float) -> float:
    rate = max(0.08 - 0.004 * SKILLS["accounting"], 0.01)
    return price * rate

def calc_broker_fee_buy(price: float) -> float:
    rate = max(0.03 - 0.001 * SKILLS["broker_relations"]
               - 0.0003 * SKILLS["advanced_broker_relations"], 0.001)
    return price * rate

def calc_broker_fee_sell(price: float) -> float:
    return price * 0.05  # Nullsec-Struktur, konservativ 5%

def calc_shipping(jita_price: float, volume_m3: float, service: str) -> float:
    s = SHIPPING[service]
    raw = s["isk_per_m3"] * volume_m3 + s["collateral_pct"] * jita_price
    return max(raw, s["min_cost"])

def calc_net_profit(jita_price: float, null_price: float,
                    volume_m3: float, service: str) -> dict:
    broker_buy  = calc_broker_fee_buy(jita_price)
    shipping    = calc_shipping(jita_price, volume_m3, service)
    broker_sell = calc_broker_fee_sell(null_price)
    sales_tax   = calc_sales_tax(null_price)
    total_cost  = jita_price + broker_buy + shipping + broker_sell + sales_tax
    profit_isk  = null_price - total_cost
    profit_pct  = (profit_isk / total_cost * 100) if total_cost > 0 else 0
    return {
        "jita_buy":    jita_price,
        "broker_buy":  broker_buy,
        "shipping":    shipping,
        "broker_sell": broker_sell,
        "sales_tax":   sales_tax,
        "total_cost":  total_cost,
        "null_sell":   null_price,
        "profit_isk":  profit_isk,
        "profit_pct":  profit_pct,
    }

# ===========================================================================
# EVE SSO
# ===========================================================================

SSO_AUTH_URL  = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
CALLBACK_PORT = 12345
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
# ESI – Hilfsfunktionen
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
        r = requests.get(f"{ESI}{path}", headers=headers,
                         params=params, timeout=30)
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
    r = requests.get(f"{ESI}{path}", headers=headers,
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()

# ===========================================================================
# BULK FETCHER – Herzstück der Performance
# ===========================================================================

def bulk_jita_orders(type_ids: set) -> dict:
    """
    Lädt ALLE Jita-Orders in ~5-10 paginierten Calls.
    Statt 500 Einzelcalls → ein Bulk-Fetch, dann lokal filtern.
    Gibt best_sell_price[type_id] zurück.
    """
    print(f"  [BULK] Lade alle Jita-Orders...")
    t0 = time.time()
    all_orders = _esi_pages(f"/markets/{THE_FORGE_REGION_ID}/orders/",
                            params={"order_type": "all"})
    print(f"  [BULK] {len(all_orders):,} Orders geladen in {time.time()-t0:.1f}s")

    best_sell = {}
    for o in all_orders:
        if o.get("location_id") != JITA_STATION_ID:
            continue
        if o["type_id"] not in type_ids:
            continue
        if not o["is_buy_order"]:
            tid = o["type_id"]
            if tid not in best_sell or o["price"] < best_sell[tid]:
                best_sell[tid] = o["price"]

    print(f"  [BULK] {len(best_sell):,} relevante Items in Jita 4-4 gefunden")
    return best_sell

def bulk_jita_history(type_ids: set) -> dict:
    """Jita-Historien für alle Items – parallel."""
    print(f"  [BULK] Jita-Historien für {len(type_ids)} Items (parallel)...")
    t0 = time.time()
    result = {}

    def fetch(tid):
        try:
            hist = _esi_pages(f"/markets/{THE_FORGE_REGION_ID}/history/",
                              params={"type_id": tid})
            if not hist:
                return tid, 0.0
            recent = sorted(hist, key=lambda x: x["date"], reverse=True)[:30]
            avg = sum(d["volume"] for d in recent) / len(recent) if recent else 0.0
            return tid, avg
        except Exception:
            return tid, 0.0

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as ex:
        for tid, vol in ex.map(fetch, type_ids):
            result[tid] = vol

    print(f"  [BULK] Jita-Historien fertig in {time.time()-t0:.1f}s")
    return result

def bulk_null_history(type_ids: set, region_id: int) -> dict:
    """Nullsec-Regionshistorien für alle Items – parallel."""
    print(f"  [BULK] Nullsec-Historien für {len(type_ids)} Items (parallel)...")
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

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as ex:
        for tid, avg, ratio in ex.map(fetch, type_ids):
            result[tid] = (avg, ratio)

    print(f"  [BULK] Nullsec-Historien fertig in {time.time()-t0:.1f}s")
    return result

# ===========================================================================
# ITEM-INFO CACHE (Disk) – einmal laden, für immer gecacht
# ===========================================================================

_item_cache: dict = {}

def load_item_cache():
    global _item_cache
    if os.path.exists(ITEM_CACHE_FILE):
        with open(ITEM_CACHE_FILE) as f:
            _item_cache = json.load(f)
        print(f"  [CACHE] {len(_item_cache):,} Items aus Disk-Cache")

def fetch_item_infos(type_ids: set) -> dict:
    """Holt Name + m³ für alle Items. Unbekannte werden parallel nachgeladen."""
    missing = {tid for tid in type_ids if str(tid) not in _item_cache}
    if missing:
        print(f"  [CACHE] {len(missing)} neue Items nachladen (parallel)...")

        def fetch(tid):
            try:
                info = _esi_get(f"/universe/types/{tid}/")
                return str(tid), {
                    "name":   info.get("name", f"Type {tid}"),
                    "volume": info.get("packaged_volume",
                              info.get("volume", 1.0)),
                }
            except Exception:
                return str(tid), {"name": f"Type {tid}", "volume": 1.0}

        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as ex:
            for key, val in ex.map(fetch, missing):
                _item_cache[key] = val

        os.makedirs(os.path.dirname(ITEM_CACHE_FILE), exist_ok=True)
        with open(ITEM_CACHE_FILE, "w") as f:
            json.dump(_item_cache, f)
        print(f"  [CACHE] Gespeichert ({len(_item_cache):,} Items total)")

    return {tid: _item_cache.get(str(tid), {"name": f"Type {tid}", "volume": 1.0})
            for tid in type_ids}

# ===========================================================================
# REGION AUFLÖSUNG
# ===========================================================================

_region_cache: dict = {}

def resolve_region(structure_id: int, token: str):
    if structure_id in _region_cache:
        return _region_cache[structure_id]
    try:
        info   = _esi_get(f"/universe/structures/{structure_id}/", token=token)
        sys    = _esi_get(f"/universe/systems/{info['solar_system_id']}/")
        con    = _esi_get(f"/universe/constellations/{sys['constellation_id']}/")
        region = con["region_id"]
        _region_cache[structure_id] = region
        print(f"  [GEO] → Region {region}")
        return region
    except Exception as e:
        print(f"  [WARN] Region-Auflösung fehlgeschlagen: {e}")
        return None

# ===========================================================================
# SCANNER
# ===========================================================================

def scan_all(access_token: str) -> list:
    total_start = time.time()
    all_deals   = []
    fake_ids    = {1234567890123, 1234567890124, 1234567890125, 1234567890126}

    for struct_name, struct_id in STRUCTURES.items():
        if struct_id in fake_ids:
            print(f"\n[SKIP] {struct_name} – Platzhalter-ID eintragen")
            continue

        service = STRUCTURE_SHIPPING.get(struct_name, "HWL")
        print(f"\n{'='*55}")
        print(f"[SCAN] {struct_name}  (via {service})")
        print(f"{'='*55}")
        t0 = time.time()

        # Region auflösen
        region_id = resolve_region(struct_id, access_token)

        # Struktur-Orders laden
        print(f"  Lade Struktur-Orders...")
        struct_orders = _esi_pages(
            f"/markets/structures/{struct_id}/", token=access_token)
        if not struct_orders:
            print(f"  Keine Orders oder kein Zugriff.")
            continue

        # Beste Kauf- und Verkaufsaufträge pro Item
        best_buy  = {}  # höchster Kaufpreis (Instant-Sell Ziel)
        best_sell = {}  # günstigster Verkaufspreis (Planned-Sell Wettbewerb)
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

        # Jita Bulk-Fetch
        jita_prices = bulk_jita_orders(type_ids)
        type_ids    = type_ids & set(jita_prices)  # nur was in Jita verfügbar ist
        if not type_ids:
            print("  Keine Überschneidung mit Jita-Sortiment.")
            continue

        # Item-Infos (gecacht)
        item_infos = fetch_item_infos(type_ids)

        # Historien parallel
        jita_hist = bulk_jita_history(type_ids)
        null_hist = bulk_null_history(type_ids, region_id) if region_id else {}

        # Deal-Berechnung komplett in RAM
        print(f"  Berechne Deals...")
        deals = []
        for tid in type_ids:
            jita_price       = jita_prices[tid]
            info             = item_infos[tid]
            volume_m3        = info["volume"]
            jita_vol         = jita_hist.get(tid, 0.0)
            null_vol, n_ratio = null_hist.get(tid, (0.0, 0.0))

            if jita_vol < MIN_DAILY_VOL_JITA:
                continue
            if region_id and null_vol < MIN_DAILY_VOL_NULL:
                continue

            base = {
                "type_id":   tid,
                "item_name": info["name"],
                "structure": struct_name,
                "service":   service,
                "volume_m3": volume_m3,
                "jita_vol":  jita_vol,
                "null_vol":  null_vol,
                "null_ratio": n_ratio,
            }

            # Instant-Sell
            if tid in best_buy:
                r = calc_net_profit(jita_price, best_buy[tid], volume_m3, service)
                if (r["profit_isk"] >= MIN_PROFIT_ISK
                        and r["profit_pct"] >= MIN_PROFIT_PCT):
                    deals.append({**base, "exit": "instant", **r})

            # Planned-Sell (0.1% unter günstigstem Wettbewerber)
            if tid in best_sell:
                target = best_sell[tid] * 0.999
                r = calc_net_profit(jita_price, target, volume_m3, service)
                if (r["profit_isk"] >= MIN_PROFIT_ISK
                        and r["profit_pct"] >= MIN_PROFIT_PCT):
                    deals.append({**base, "exit": "planned", **r})

        print(f"  → {len(deals)} profitable Deals  ({time.time()-t0:.1f}s)")
        all_deals.extend(deals)

    print(f"\n[FERTIG] Gesamtzeit: {time.time()-total_start:.1f}s")
    return all_deals

# ===========================================================================
# AUSGABE
# ===========================================================================

def _activity(ratio: float) -> str:
    if ratio >= 0.8: return "🟢 aktiv"
    if ratio >= 0.4: return "🟡 gelegentlich"
    if ratio >  0:   return "🔴 selten"
    return "⚪ keine Daten"

def print_deal(d: dict):
    tag = "[INSTANT]" if d["exit"] == "instant" else "[PLANNED]"
    print(f"\n  {tag} {d['item_name']}")
    print(f"    Struktur:        {d['structure']}  (via {d['service']})")
    print(f"    Jita Kauf:       {d['jita_buy']:>15,.0f} ISK")
    print(f"    Broker Fee:      {d['broker_buy']:>15,.0f} ISK")
    print(f"    Lieferkosten:    {d['shipping']:>15,.0f} ISK  ({d['volume_m3']:.2f} m³)")
    print(f"    Null Verkauf:    {d['null_sell']:>15,.0f} ISK")
    print(f"    Broker + Tax:    {d['broker_sell'] + d['sales_tax']:>15,.0f} ISK")
    print(f"    {'─'*42}")
    print(f"    Nettogewinn:     {d['profit_isk']:>15,.0f} ISK  ({d['profit_pct']:.1f}%)")
    print(f"    Jita Vol:        ~{d['jita_vol']:>8,.0f} / Tag")
    if d["null_vol"] > 0:
        print(f"    Null Vol:        ~{d['null_vol']:>8,.1f} / Tag  "
              f"{_activity(d['null_ratio'])}  "
              f"({d['null_ratio']*100:.0f}% aktiv / 30 Tage)")
    else:
        print(f"    Null Vol:        keine Regionsdaten")

def print_results(deals: list):
    if not deals:
        print("\n[RESULT] Keine profitablen Deals gefunden.")
        print("  Tipp: MIN_PROFIT_ISK / MIN_PROFIT_PCT in der Config reduzieren.")
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
    for s, ds in by_s.items():
        best = max(ds, key=lambda x: x["profit_isk"])
        print(f"  {s}: {len(ds)} Deals | "
              f"Bester: {best['item_name']} "
              f"+{best['profit_isk']:,.0f} ISK ({best['profit_pct']:.1f}%)")

# ===========================================================================
# MAIN
# ===========================================================================

def main():
    print("=" * 55)
    print("  EVE Nullsec Price Scanner – Performance Edition")
    print("=" * 55)

    if CLIENT_ID == "DEINE_CLIENT_ID_HIER":
        print("\n[FEHLER] CLIENT_ID fehlt!")
        print(f"  1. https://developers.eveonline.com/ → neue App anlegen")
        print(f"  2. Callback URL: http://localhost:{CALLBACK_PORT}/callback")
        print(f"  3. Scope: {SCOPE}")
        print(f"  4. Client ID oben im Script eintragen")
        return

    os.makedirs("cache", exist_ok=True)
    load_item_cache()

    print("\n[AUTH] Token prüfen...")
    tok = get_valid_token()
    print(f"[AUTH] OK  |  "
          f"Sales Tax: {max(0.08 - 0.004*SKILLS['accounting'], 0.01)*100:.1f}%  |  "
          f"Broker Fee Jita: {max(0.03 - 0.001*SKILLS['broker_relations'], 0.001)*100:.1f}%")

    deals = scan_all(tok["access_token"])
    print_results(deals)

    if deals:
        out = f"scan_result_{int(time.time())}.json"
        with open(out, "w") as f:
            json.dump(deals, f, indent=2)
        print(f"\n[SAVE] {out}")

if __name__ == "__main__":
    main()
