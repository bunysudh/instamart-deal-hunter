import base64, hashlib, json, os, re, secrets, sqlite3, threading, time, webbrowser, statistics
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs
import httpx2

BASE = "https://mcp.swiggy.com"
URL = BASE + "/im"
REDIRECT = "http://127.0.0.1:3030/callback"
TOKEN_FILE = "swiggy_token.json"
DB_FILE = "instamart_prices.db"
DEALS_FILE = "rare_deals.json"
ALERT_LOG = "rare_deal_alerts.log"

SEARCHES = ['snacks', 'chips', 'namkeen', 'biscuits', 'cookies', 'chocolate', 'candy', 'sweets', 'popcorn', 'noodles', 'pasta', 'vermicelli', 'soup', 'sauce', 'ketchup', 'mayonnaise', 'spread', 'jam', 'instant food', 'ready to eat', 'frozen food', 'frozen snacks', 'beverages', 'soft drinks', 'juice', 'fruit juice', 'energy drink', 'water', 'tea', 'coffee', 'green tea', 'milk', 'curd', 'yogurt', 'butter', 'cheese', 'paneer', 'cream', 'bread', 'bun', 'cake', 'rusk', 'croissant', 'atta', 'flour', 'rice', 'basmati rice', 'dal', 'lentils', 'poha', 'rava', 'sooji', 'oats', 'cereal', 'breakfast', 'sugar', 'salt', 'jaggery', 'honey', 'oil', 'ghee', 'spices', 'masala', 'turmeric', 'chilli powder', 'cumin', 'pepper', 'garam masala', 'pickle', 'papad', 'dry fruits', 'nuts', 'seeds', 'personal care', 'soap', 'body wash', 'shampoo', 'conditioner', 'hair oil', 'face wash', 'skin care', 'moisturizer', 'sunscreen', 'toothpaste', 'toothbrush', 'mouthwash', 'deodorant', 'perfume', 'shaving', 'razor', 'feminine care', 'sanitary pads', 'household', 'cleaning', 'floor cleaner', 'toilet cleaner', 'bathroom cleaner', 'kitchen cleaner', 'dishwash', 'dish soap', 'detergent', 'washing powder', 'air freshener', 'tissue', 'toilet tissue', 'paper towels', 'garbage bags', 'aluminium foil', 'storage bags', 'baby care', 'baby food', 'baby diapers', 'diapers', 'baby wipes', 'baby shampoo', 'baby soap', 'baby lotion', 'pet care', 'pet food', 'dog food', 'cat food', 'pet treats', 'pet litter', 'meat', 'chicken', 'mutton', 'fish', 'seafood', 'health care', 'vitamins', 'nutrition', 'protein', 'supplements', 'first aid', 'wellness', 'medical devices', 'electronics', 'mobile accessories', 'charger', 'cable', 'earphones', 'batteries', 'light bulbs', 'led bulb', 'stationery', 'school supplies', 'kitchen', 'kitchen tools', 'storage', 'home utility', 'clothing', 'socks', 'travel', 'umbrella', 'toys', 'games', 'gifts', 'plants', 'indoor plants', 'outdoor plants', 'flowering plants', 'herbs', 'succulents', 'garden', 'gardening', 'plant pots', 'planters', 'seeds', 'gardening tools']

cb = {}
TOKEN = None

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        cb["code"] = q.get("code", [None])[0]
        cb["state"] = q.get("state", [None])[0]
        cb["error"] = q.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Swiggy authentication received. You can close this window.</h2>")
    def log_message(self, *a):
        pass

def server():
    s = HTTPServer(("127.0.0.1", 3030), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s

def parse(r):
    if "application/json" in r.headers.get("content-type", "").lower():
        return r.json()
    for x in r.text.splitlines():
        if x.startswith("data:"):
            try:
                return json.loads(x[5:].strip())
            except Exception:
                pass
    raise RuntimeError(r.text[:2000])

def auth():
    payload = {"client_name":"Instamart Deal Scanner", "redirect_uris":[REDIRECT],
               "grant_types":["authorization_code"], "response_types":["code"],
               "token_endpoint_auth_method":"none"}
    with httpx2.Client(timeout=30) as c:
        cid = c.post(BASE + "/auth/register", json=payload).json()["client_id"]
    v = secrets.token_urlsafe(32)
    ch = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    st = secrets.token_urlsafe(24)
    cb.clear()
    webbrowser.open(BASE + "/auth/authorize?" + urlencode({
        "response_type":"code", "client_id":cid, "redirect_uri":REDIRECT,
        "code_challenge":ch, "code_challenge_method":"S256", "state":st, "scope":"mcp:tools"}))
    print("Browser authentication required. Complete Swiggy login/OTP.")
    end = time.time() + 300
    while "code" not in cb and "error" not in cb:
        if time.time() > end:
            raise TimeoutError("Authentication timed out.")
        time.sleep(.5)
    if cb.get("error") or cb.get("state") != st:
        raise RuntimeError("Swiggy authentication failed.")
    with httpx2.Client(timeout=30) as c:
        r = c.post(BASE + "/auth/token", json={"grant_type":"authorization_code",
            "code":cb["code"], "code_verifier":v, "redirect_uri":REDIRECT})
    r.raise_for_status()
    d = r.json()
    token = d["access_token"]
    exp = int(time.time()) + int(d.get("expires_in", 432000)) - 300
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token":token, "expires_at":exp}, f)
    return token

def token():
    try:
        with open(TOKEN_FILE) as f:
            d = json.load(f)
        if time.time() < d["expires_at"]:
            print("Using saved Swiggy token.")
            return d["access_token"]
    except Exception:
        pass
    print("No valid saved token; authenticating once.")
    return auth()

def req(c, sid, rid, method, params=None):
    h = {"Authorization":"Bearer "+TOKEN, "Content-Type":"application/json",
         "Accept":"application/json, text/event-stream", "MCP-Protocol-Version":"2025-06-18"}
    if sid:
        h["Mcp-Session-Id"] = sid
    x = {"jsonrpc":"2.0", "id":rid, "method":method}
    if params is not None:
        x["params"] = params
    for attempt in range(3):
        r = c.post(URL, headers=h, json=x, timeout=60)
        if r.status_code != 429 or attempt == 2:
            if r.status_code in (401,403):
                raise RuntimeError("Swiggy token expired or unauthorized.")
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:2000]}")
            return r, parse(r)
        wait = 2 ** (attempt + 1)
        print(f"  Rate limited (429); waiting {wait}s...")
        time.sleep(wait)
    raise RuntimeError("Unexpected request failure.")

def call(c, sid, rid, name, args):
    return req(c, sid, rid, "tools/call", {"name":name, "arguments":args})

def products(res):
    return res.get("result", {}).get("structuredContent", {}).get("products", [])

def addresses(res):
    return res.get("result", {}).get("structuredContent", {}).get("addresses", [])

def init_session():
    h = {"Authorization":"Bearer "+TOKEN, "Content-Type":"application/json",
         "Accept":"application/json, text/event-stream", "MCP-Protocol-Version":"2025-06-18"}
    init = {"jsonrpc":"2.0", "id":1, "method":"initialize",
            "params":{"protocolVersion":"2025-06-18","capabilities":{},
                      "clientInfo":{"name":"instamart-rare-deal-scanner","version":"1.0.0"}}}
    c = httpx2.Client(timeout=60)
    r = c.post(URL, headers=h, json=init)
    if r.status_code in (401,403):
        c.close()
        raise RuntimeError("Saved token expired or unauthorized. Re-run after authentication.")
    r.raise_for_status()
    parse(r)
    sid = r.headers.get("mcp-session-id")
    nh = dict(h)
    if sid:
        nh["Mcp-Session-Id"] = sid
    c.post(URL, headers=nh, json={"jsonrpc":"2.0","method":"notifications/initialized","params":{}})
    return c, sid

def product_key(p, v):
    # Location-specific Swiggy IDs are intentionally NOT used for cross-PIN matching.
    vals = [p.get("displayName",""), v.get("quantityDescription","")]
    return "|".join(str(x).strip().lower() for x in vals)

def normalize(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(s or "").lower())).strip()

def cross_key(p, v):
    # Cross-PIN matching deliberately ignores location-specific Swiggy IDs.
    # Brand + exact display name + exact pack is the safest text-level match.
    return (normalize(p.get("brand","")), normalize(p.get("displayName","")),
            normalize(v.get("quantityDescription","")))

def extract_rows(ps, search_term):
    out = {}
    for p in ps:
        for v in p.get("variations", []):
            pr = v.get("price", {}) or {}
            mrp, op = pr.get("mrp"), pr.get("offerPrice")
            if not isinstance(mrp, (int,float)) or not isinstance(op, (int,float)):
                continue
            if mrp <= 0 or op < 0 or op > mrp:
                continue
            row = {
                "product_id": str(p.get("productId","")),
                "sku_id": str(v.get("skuId","")),
                "spin_id": str(v.get("spinId","")),
                "variation_id": str(v.get("variationId","")),
                "brand": p.get("brand",""),
                "name": p.get("displayName","Unknown"),
                "pack": v.get("quantityDescription",""),
                "mrp": float(mrp),
                "offer": float(op),
                "unit_price": str(pr.get("unitLevelPrice","")),
                "in_stock": bool(v.get("isInStockAndAvailable", False)),
                "search_term": search_term,
            }
            k = cross_key(p,v)
            # Keep one row per exact name + pack, preferring in-stock.
            if k not in out or (row["in_stock"] and not out[k]["in_stock"]):
                out[k] = row
    return out

def db_init():
    d = sqlite3.connect(DB_FILE)
    d.execute("""CREATE TABLE IF NOT EXISTS price_history(
        id INTEGER PRIMARY KEY, scan_time TEXT, product_key TEXT, product_id TEXT,
        sku_id TEXT, spin_id TEXT, variation_id TEXT, brand TEXT, product_name TEXT,
        pack TEXT, mrp REAL, offer_price REAL, unit_price TEXT, discount_pct REAL,
        in_stock INTEGER, search_term TEXT)""")
    d.execute("CREATE INDEX IF NOT EXISTS ix_ph ON price_history(product_key, scan_time)")
    d.execute("""CREATE TABLE IF NOT EXISTS comparison_history(
        id INTEGER PRIMARY KEY, scan_time TEXT, comparison_pin TEXT, product_key TEXT,
        brand TEXT, product_name TEXT, pack TEXT, mrp REAL, offer_price REAL,
        unit_price TEXT, in_stock INTEGER, search_term TEXT)""")
    d.execute("CREATE INDEX IF NOT EXISTS ix_ch ON comparison_history(product_key, comparison_pin, scan_time)")
    d.execute("""CREATE TABLE IF NOT EXISTS deal_alerts(
        id INTEGER PRIMARY KEY, alert_time TEXT, product_key TEXT, product_name TEXT,
        pack TEXT, current_price REAL, historical_median REAL, historical_low REAL,
        historical_p20 REAL, comparison_price REAL, comparison_pin TEXT,
        score REAL, reason TEXT)""")
    d.commit()
    return d

def save_main(d, rows):
    d.executemany("""INSERT INTO price_history(
        scan_time,product_key,product_id,sku_id,spin_id,variation_id,brand,
        product_name,pack,mrp,offer_price,unit_price,discount_pct,in_stock,search_term)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    d.commit()

def save_comparison(d, now, rows):
    data = []
    for k, r in rows.items():
        data.append((now, "500084", "|".join(k), r["brand"], r["name"], r["pack"],
                     r["mrp"], r["offer"], r["unit_price"], 1 if r["in_stock"] else 0, r["search_term"]))
    d.executemany("""INSERT INTO comparison_history(
        scan_time,comparison_pin,product_key,brand,product_name,pack,mrp,offer_price,
        unit_price,in_stock,search_term) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", data)
    d.commit()

def percentile(vals, p):
    vals = sorted(vals)
    if not vals: return None
    if len(vals) == 1: return vals[0]
    k = (len(vals)-1) * p
    f = int(k); c = min(f+1, len(vals)-1)
    if f == c: return vals[f]
    return vals[f] + (vals[c]-vals[f])*(k-f)

def historical_stats(d, row, before_time):
    # IMPORTANT: Never use product_id alone for history. Swiggy can reuse or
    # change identifiers across variants/locations. Match exact normalized
    # brand + product name + pack instead.
    #
    # Reject historical observations whose MRP is materially higher than
    # today's MRP. This prevents cases such as current MRP ₹90 with an old
    # ₹120 observation becoming a false "typical" price.
    cur = d.execute("""SELECT offer_price, mrp FROM price_history
                       WHERE lower(coalesce(brand,''))=?
                       AND lower(coalesce(product_name,''))=?
                       AND lower(coalesce(pack,''))=?
                       AND scan_time < ?
                       AND offer_price IS NOT NULL AND offer_price >= 0
                       AND mrp IS NOT NULL AND mrp > 0
                       ORDER BY scan_time""",
                    (normalize(row["brand"]), normalize(row["name"]),
                     normalize(row["pack"]), before_time))

    vals = []
    current_mrp = float(row["mrp"])

    for offer, mrp in cur.fetchall():
        offer = float(offer)
        mrp = float(mrp)

        # Ignore historical observations from a materially different MRP.
        # Keep a small 5% tolerance for data noise.
        if mrp > current_mrp * 1.05:
            continue
        if offer > mrp:
            continue
        vals.append(offer)

    if len(vals) < 3:
        return None

    return {
        "n": len(vals),
        "median": statistics.median(vals),
        "low": min(vals),
        "p20": percentile(vals, .20),
        "p10": percentile(vals, .10),
    }

def previous_alerted(d, key, current_price):
    row = d.execute("""SELECT current_price FROM deal_alerts
                       WHERE product_key=? ORDER BY id DESC LIMIT 1""", (key,)).fetchone()
    if not row:
        return False
    # Avoid repeating the same alert every hour unless the price gets lower.
    return current_price >= float(row[0])

def evaluate_deals(d, now, main_rows, comp_rows):
    deals = []
    for k, r in main_rows.items():
        if not r["in_stock"]:
            continue
        stats = historical_stats(d, r, now)
        if not stats:
            continue

        cur = r["offer"]
        median = stats["median"]
        low = stats["low"]
        p20 = stats["p20"]

        # Safety guard: a historical "typical" price above today's MRP is
        # not credible evidence of a discount. Do not alert.
        if median > r["mrp"] * 1.05:
            continue

        # Primary criterion: current 533006 price must be genuinely low
        # versus its own historical price, not merely low versus MRP.
        hist_drop = (median-cur)/median if median else 0
        at_low = cur <= low * 1.02
        below_p20 = cur <= p20
        strong_hist = hist_drop >= .25 and below_p20
        if not (strong_hist or (hist_drop >= .35 and at_low)):
            continue

        comp = comp_rows.get(k)
        comp_price = comp["offer"] if comp and comp["in_stock"] else None
        cross_support = comp_price is not None and comp_price >= cur * 1.10

        # Require cross-PIN support for the alert.
        if not cross_support:
            continue

        score = 50 + min(30, hist_drop*100) + (10 if at_low else 0) + (10 if comp_price >= cur*1.20 else 0)
        reasons = [
            f"current ₹{cur:.0f} vs historical median ₹{median:.0f}",
            f"historical low ₹{low:.0f}",
            f"500084 ₹{comp_price:.0f}"
        ]
        if at_low: reasons.append("near historical low")
        if below_p20: reasons.append("at/below historical 20th percentile")

        key = "|".join(k)
        if previous_alerted(d, key, cur):
            continue

        deal = {
            "alert_time": now, "product_key": key, "product_name": r["name"],
            "pack": r["pack"], "current_price_533006": cur,
            "historical_median": round(median,2), "historical_low": round(low,2),
            "historical_p20": round(p20,2), "comparison_pin":"500084",
            "comparison_price": comp_price, "historical_observations":stats["n"],
            "score": round(score,1), "reason": "; ".join(reasons)
        }
        deals.append(deal)

    if deals:
        d.executemany("""INSERT INTO deal_alerts(
            alert_time,product_key,product_name,pack,current_price,historical_median,
            historical_low,historical_p20,comparison_price,comparison_pin,score,reason)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(x["alert_time"],x["product_key"],x["product_name"],x["pack"],
          x["current_price_533006"],x["historical_median"],x["historical_low"],
          x["historical_p20"],x["comparison_price"],x["comparison_pin"],
          x["score"],x["reason"]) for x in deals])
        d.commit()
    return deals

def run_scan(c, sid, aid_main, aid_comp):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    main = {}
    comp = {}
    pages = 0
    failed = 0

    print("\n--- 533006 MAIN SCAN ---")
    for i, q in enumerate(SEARCHES, 1):
        print(f"[533006 {i:03}/{len(SEARCHES)}] {q}")
        try:
            _, res = call(c, sid, 10+i, "search_products",
                          {"addressId":aid_main, "query":q, "offset":0})
            pages += 1
            main.update(extract_rows(products(res), q))
        except Exception as e:
            failed += 1
            print("  FAILED:", e)
        time.sleep(.15)

    d = db_init()
    try:
        rows = []
        for k, r in main.items():
            rows.append((now, "|".join(k), r["product_id"], r["sku_id"],
                         r["spin_id"], r["variation_id"], r["brand"], r["name"],
                         r["pack"], r["mrp"], r["offer"], r["unit_price"],
                         round((r["mrp"]-r["offer"])/r["mrp"]*100,2),
                         1 if r["in_stock"] else 0, r["search_term"]))
        save_main(d, rows)
    finally:
        d.close()

    print("\n--- 500084 COMPARISON SCAN ---")
    for i, q in enumerate(SEARCHES, 1):
        print(f"[500084 {i:03}/{len(SEARCHES)}] {q}")
        try:
            _, res = call(c, sid, 500+i, "search_products",
                          {"addressId":aid_comp, "query":q, "offset":0})
            pages += 1
            comp.update(extract_rows(products(res), q))
        except Exception as e:
            failed += 1
            print("  FAILED:", e)
        time.sleep(.15)

    d = db_init()
    try:
        save_comparison(d, now, comp)
        deals = evaluate_deals(d, now, main, comp)
    finally:
        d.close()

    with open(DEALS_FILE, "w", encoding="utf-8") as f:
        json.dump(deals, f, indent=2, ensure_ascii=False)

    if deals:
        with open(ALERT_LOG, "a", encoding="utf-8") as f:
            for x in deals:
                line = (f'{now} | RARE DEAL | {x["product_name"]} | {x["pack"]} | '
                        f'533006 ₹{x["current_price_533006"]:.0f} | '
                        f'median ₹{x["historical_median"]:.0f} | '
                        f'500084 ₹{x["comparison_price"]:.0f} | score {x["score"]:.1f}\n')
                f.write(line)

    print("\n" + "="*70)
    print("SCAN COMPLETE")
    print("="*70)
    print(f"Timestamp: {now}")
    print(f"533006 unique variants: {len(main)}")
    print(f"500084 unique name+pack variants: {len(comp)}")
    print(f"API pages: {pages}")
    print(f"Failed searches: {failed}")
    print(f"New RARE DEALS: {len(deals)}")
    print(f"History DB: {DB_FILE}")
    print(f"Latest deals: {DEALS_FILE}")
    if deals:
        print("\n*** NEW RARE DEALS ***")
        for x in deals[:20]:
            print(f'RARE DEAL — {x["product_name"]} | {x["pack"]} | '
                  f'533006 ₹{x["current_price_533006"]:.0f} | '
                  f'typical ₹{x["historical_median"]:.0f} | '
                  f'500084 ₹{x["comparison_price"]:.0f} | score {x["score"]:.1f}')
    else:
        print("No new rare deal alerts this hour.")

def main():
    global TOKEN
    print("="*70)
    print(" INSTAMART RARE-DEAL SCANNER — 533006 vs 500084")
    print("="*70)
    print("Hourly scan. Existing history is preserved.")
    print("Primary: 533006 historical price. Supporting: 500084 current price.")
    print("Alerts require historical rarity + cross-PIN support + MRP sanity checks.")
    print("Excluded: fresh fruits/vegetables/leafy greens, Paan, Sexual Wellness, Puja.")
    print("No cart/order changes.")
    print("="*70)

    s = server()
    try:
        TOKEN = token()
        while True:
            started = time.time()
            c = None
            try:
                c, sid = init_session()
                _, ar = call(c, sid, 2, "get_addresses", {"page":1, "pageSize":10})
                aa = addresses(ar)

                main_addr = next((a for a in aa if str(a.get("addressTag","")).lower()=="home"
                                  and "533006" in str(a)), None)
                if not main_addr:
                    main_addr = next((a for a in aa if "533006" in str(a)), None)

                comp_addr = next((a for a in aa if "500084" in str(a)), None)

                if not main_addr:
                    raise RuntimeError("533006 address not found.")
                if not comp_addr:
                    raise RuntimeError("500084 comparison address not found.")

                print("533006 main address found.")
                print("500084 comparison address found.")
                run_scan(c, sid, main_addr["id"], comp_addr["id"])
            except Exception as e:
                print("="*70)
                print("SCAN ERROR")
                print("="*70)
                print(e)
                print("The hourly loop will retry on the next cycle.")
            finally:
                if c:
                    c.close()

            elapsed = time.time() - started
            wait = max(60, 3600-elapsed)
            next_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()+wait))
            print("="*70)
            print(f"Next scan: approximately {next_time}")
            print(f"Waiting {int(wait//60)} minutes.")
            print("="*70)
            time.sleep(wait)
    finally:
        s.shutdown()

if __name__ == "__main__":
    main()
