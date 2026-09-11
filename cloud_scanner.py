import os
import json
import time
import importlib.util
from datetime import datetime, timezone, timedelta

import httpx2


# ============================================================
# CLOUD CONFIG
# ============================================================

SWIGGY_TOKEN = os.environ["SWIGGY_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

MAIN_PIN = "533006"
COMPARISON_PIN = "500084"

SCANNER_FILE = "instamart_final_533006_500084_v2.py"
DEALS_FILE = "rare_deals.json"

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": "Bearer " + SUPABASE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}


# ============================================================
# LOAD THE TESTED SWIGGY SCANNER MODULE
# ============================================================

spec = importlib.util.spec_from_file_location(
    "local_scanner",
    SCANNER_FILE
)

sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)

# The original scanner's MCP functions use this global.
sw.TOKEN = SWIGGY_TOKEN


# ============================================================
# SUPABASE HELPERS
# ============================================================

def supabase_rpc(function_name, payload):
    url = f"{SUPABASE_URL}/rest/v1/rpc/{function_name}"

    with httpx2.Client(timeout=120) as client:
        r = client.post(
            url,
            headers=SB_HEADERS,
            json=payload,
        )

    if r.status_code >= 400:
        raise RuntimeError(
            f"Supabase RPC {function_name} failed: "
            f"HTTP {r.status_code}: {r.text[:1000]}"
        )

    if not r.text.strip():
        return []

    return r.json()


def supabase_insert(table, rows):
    if not rows:
        return

    url = f"{SUPABASE_URL}/rest/v1/{table}"

    # Keep requests comfortably sized.
    for start in range(0, len(rows), 500):
        chunk = rows[start:start + 500]

        with httpx2.Client(timeout=120) as client:
            r = client.post(
                url,
                headers=SB_HEADERS,
                json=chunk,
            )

        if r.status_code >= 400:
            raise RuntimeError(
                f"Supabase insert into {table} failed: "
                f"HTTP {r.status_code}: {r.text[:1000]}"
            )


# ============================================================
# HELPERS
# ============================================================

def iso_now():
    return datetime.now(timezone.utc).isoformat()


def key_string(key):
    return "|".join(key)


def price_changed(old_price, new_price):
    if old_price is None:
        return True

    try:
        return abs(float(old_price) - float(new_price)) > 0.001
    except Exception:
        return True


def should_store(latest, current, now_dt):
    """
    Store:
      - first observation
      - any price/MRP change
      - at least one observation every 24 hours

    This keeps hourly scanning while avoiding millions of identical
    database rows when a product price stays unchanged.
    """

    if not latest:
        return True

    if price_changed(latest.get("offer_price"), current.get("offer")):
        return True

    if price_changed(latest.get("mrp"), current.get("mrp")):
        return True

    try:
        old_time = datetime.fromisoformat(
            str(latest["scan_time"]).replace("Z", "+00:00")
        )

        if now_dt - old_time >= timedelta(hours=24):
            return True

    except Exception:
        return True

    return False


# ============================================================
# SUPABASE HISTORY LOOKUPS
# ============================================================

def get_latest_main(keys):
    if not keys:
        return {}

    rows = supabase_rpc(
        "get_latest_main_prices",
        {"p_product_keys": keys},
    )

    return {
        str(x["product_key"]): x
        for x in rows
    }


def get_latest_comparison(keys):
    if not keys:
        return {}

    rows = supabase_rpc(
        "get_latest_comparison_prices",
        {
            "p_product_keys": keys,
            "p_pin": COMPARISON_PIN,
        },
    )

    return {
        str(x["product_key"]): x
        for x in rows
    }


def get_history_stats(main_rows, before_time):
    items = []

    for key, row in main_rows.items():
        if not row["in_stock"]:
            continue

        if row["mrp"] <= 0:
            continue

        items.append({
            "product_key": key_string(key),
            "current_mrp": float(row["mrp"]),
        })

    if not items:
        return {}

    rows = supabase_rpc(
        "get_history_stats",
        {
            "p_items": items,
            "p_before": before_time,
        },
    )

    return {
        str(x["product_key"]): x
        for x in rows
    }


def get_latest_alerts(keys):
    if not keys:
        return {}

    rows = supabase_rpc(
        "get_latest_alerts",
        {"p_product_keys": keys},
    )

    return {
        str(x["product_key"]): x
        for x in rows
    }


# ============================================================
# SAVE CURRENT OBSERVATIONS
# ============================================================

def save_current_history(now, main_rows, comp_rows):
    main_keys = [key_string(k) for k in main_rows]
    comp_keys = [key_string(k) for k in comp_rows]

    latest_main = get_latest_main(main_keys)
    latest_comp = get_latest_comparison(comp_keys)

    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))

    main_to_save = []
    comp_to_save = []

    for key, r in main_rows.items():

        db_key = key_string(key)

        current = {
            "offer": r["offer"],
            "mrp": r["mrp"],
        }

        if not should_store(
            latest_main.get(db_key),
            current,
            now_dt,
        ):
            continue

        main_to_save.append({
            "scan_time": now,
            "product_key": db_key,
            "product_id": r["product_id"],
            "sku_id": r["sku_id"],
            "spin_id": r["spin_id"],
            "variation_id": r["variation_id"],
            "brand": r["brand"],
            "product_name": r["name"],
            "pack": r["pack"],
            "mrp": r["mrp"],
            "offer_price": r["offer"],
            "unit_price": r["unit_price"],
            "discount_pct": round(
                (r["mrp"] - r["offer"]) / r["mrp"] * 100,
                2,
            ),
            "in_stock": r["in_stock"],
            "search_term": r["search_term"],
        })

    for key, r in comp_rows.items():

        db_key = key_string(key)

        current = {
            "offer": r["offer"],
            "mrp": r["mrp"],
        }

        if not should_store(
            latest_comp.get(db_key),
            current,
            now_dt,
        ):
            continue

        comp_to_save.append({
            "scan_time": now,
            "comparison_pin": COMPARISON_PIN,
            "product_key": db_key,
            "brand": r["brand"],
            "product_name": r["name"],
            "pack": r["pack"],
            "mrp": r["mrp"],
            "offer_price": r["offer"],
            "unit_price": r["unit_price"],
            "in_stock": r["in_stock"],
            "search_term": r["search_term"],
        })

    supabase_insert(
        "price_history",
        main_to_save,
    )

    supabase_insert(
        "comparison_history",
        comp_to_save,
    )

    print(
        f"Saved history: {len(main_to_save)} main + "
        f"{len(comp_to_save)} comparison observations."
    )


# ============================================================
# DEAL ENGINE
# ============================================================

def evaluate_deals(main_rows, comp_rows, stats, latest_alerts):
    deals = []

    for key, row in main_rows.items():

        if not row["in_stock"]:
            continue

        db_key = key_string(key)

        s = stats.get(db_key)

        if not s:
            continue

        n = int(s["n"])
        median = float(s["median"])
        low = float(s["low"])
        p20 = float(s["p20"])

        current = float(row["offer"])
        mrp = float(row["mrp"])

        if n < 3:
            continue

        # MRP sanity guard.
        if median > mrp * 1.05:
            continue

        if median <= 0:
            continue

        hist_drop = (median - current) / median

        at_low = current <= low * 1.02
        below_p20 = current <= p20

        strong_hist = (
            hist_drop >= 0.25
            and below_p20
        )

        very_strong_hist = (
            hist_drop >= 0.35
            and at_low
        )

        if not (strong_hist or very_strong_hist):
            continue

        comparison = comp_rows.get(key)

        if not comparison:
            continue

        if not comparison["in_stock"]:
            continue

        comparison_price = float(comparison["offer"])

        cross_support = (
            comparison_price >= current * 1.10
        )

        if not cross_support:
            continue

        score = (
            50
            + min(30, hist_drop * 100)
            + (10 if at_low else 0)
            + (
                10
                if comparison_price >= current * 1.20
                else 0
            )
        )

        previous = latest_alerts.get(db_key)

        if previous:
            try:
                previous_price = float(
                    previous["current_price"]
                )

                # Do not repeat the same alert.
                # Alert again only if price becomes lower.
                if current >= previous_price:
                    continue

            except Exception:
                pass

        reasons = [
            f"current ₹{current:.0f} vs historical median ₹{median:.0f}",
            f"historical low ₹{low:.0f}",
            f"500084 ₹{comparison_price:.0f}",
        ]

        if at_low:
            reasons.append("near historical low")

        if below_p20:
            reasons.append(
                "at/below historical 20th percentile"
            )

        deals.append({
            "alert_time": iso_now(),
            "product_key": db_key,
            "product_name": row["name"],
            "pack": row["pack"],
            "current_price_533006": current,
            "historical_median": round(median, 2),
            "historical_low": round(low, 2),
            "historical_p20": round(p20, 2),
            "comparison_pin": COMPARISON_PIN,
            "comparison_price": comparison_price,
            "historical_observations": n,
            "score": round(score, 1),
            "reason": "; ".join(reasons),
        })

    return deals


def save_deals(deals):
    if not deals:
        return

    rows = []

    for d in deals:
        rows.append({
            "alert_time": d["alert_time"],
            "product_key": d["product_key"],
            "product_name": d["product_name"],
            "pack": d["pack"],
            "current_price": d["current_price_533006"],
            "historical_median": d["historical_median"],
            "historical_low": d["historical_low"],
            "historical_p20": d["historical_p20"],
            "comparison_price": d["comparison_price"],
            "comparison_pin": d["comparison_pin"],
            "score": d["score"],
            "reason": d["reason"],
        })

    supabase_insert(
        "deal_alerts",
        rows,
    )


# ============================================================
# SWIGGY SCAN
# ============================================================

def scan_pin(c, sid, address_id, pin_label, request_start):
    results = {}
    pages = 0
    failed = 0

    for i, query in enumerate(sw.SEARCHES, 1):

        print(
            f"[{pin_label} {i:03}/{len(sw.SEARCHES)}] "
            f"{query}"
        )

        try:
            _, response = sw.call(
                c,
                sid,
                request_start + i,
                "search_products",
                {
                    "addressId": address_id,
                    "query": query,
                    "offset": 0,
                },
            )

            pages += 1

            results.update(
                sw.extract_rows(
                    sw.products(response),
                    query,
                )
            )

        except Exception as e:
            failed += 1
            print(
                f"  FAILED: {str(e)[:300]}"
            )

        time.sleep(0.15)

    return results, pages, failed


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print(" INSTAMART CLOUD RARE-DEAL SCANNER")
    print(" 533006 vs 500084")
    print("=" * 70)

    now = iso_now()

    c = None

    try:

        c, sid = sw.init_session()

        # ----------------------------------------------------
        # Find saved addresses
        # ----------------------------------------------------

        _, address_response = sw.call(
            c,
            sid,
            2,
            "get_addresses",
            {
                "page": 1,
                "pageSize": 10,
            },
        )

        all_addresses = sw.addresses(
            address_response
        )

        main_addr = next(
            (
                a for a in all_addresses
                if str(a.get("addressTag", "")).lower() == "home"
                and MAIN_PIN in str(a)
            ),
            None,
        )

        if not main_addr:
            main_addr = next(
                (
                    a for a in all_addresses
                    if MAIN_PIN in str(a)
                ),
                None,
            )

        comparison_addr = next(
            (
                a for a in all_addresses
                if COMPARISON_PIN in str(a)
            ),
            None,
        )

        if not main_addr:
            raise RuntimeError(
                f"{MAIN_PIN} address not found."
            )

        if not comparison_addr:
            raise RuntimeError(
                f"{COMPARISON_PIN} comparison address not found."
            )

        print(f"{MAIN_PIN} main address found.")
        print(
            f"{COMPARISON_PIN} comparison address found."
        )

        # ----------------------------------------------------
        # Main PIN scan
        # ----------------------------------------------------

        print("\n--- 533006 MAIN SCAN ---")

        main_rows, main_pages, main_failed = scan_pin(
            c,
            sid,
            main_addr["id"],
            MAIN_PIN,
            100,
        )

        # ----------------------------------------------------
        # Comparison PIN scan
        # ----------------------------------------------------

        print("\n--- 500084 COMPARISON SCAN ---")

        comp_rows, comp_pages, comp_failed = scan_pin(
            c,
            sid,
            comparison_addr["id"],
            COMPARISON_PIN,
            500,
        )

        # ----------------------------------------------------
        # Historical statistics MUST be calculated before
        # today's observation is inserted.
        # ----------------------------------------------------

        print("\nCalculating historical statistics...")

        stats = get_history_stats(
            main_rows,
            now,
        )

        print(
            f"Historical statistics available for "
            f"{len(stats)} variants."
        )

        # ----------------------------------------------------
        # Save current observation
        # ----------------------------------------------------

        save_current_history(
            now,
            main_rows,
            comp_rows,
        )

        # ----------------------------------------------------
        # Previous alerts
        # ----------------------------------------------------

        main_keys = [
            key_string(k)
            for k in main_rows
        ]

        latest_alerts = get_latest_alerts(
            main_keys
        )

        # ----------------------------------------------------
        # Evaluate
        # ----------------------------------------------------

        deals = evaluate_deals(
            main_rows,
            comp_rows,
            stats,
            latest_alerts,
        )

        # ----------------------------------------------------
        # Save alerts
        # ----------------------------------------------------

        save_deals(deals)

        with open(
            DEALS_FILE,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                deals,
                f,
                indent=2,
                ensure_ascii=False,
            )

        # ----------------------------------------------------
        # Summary
        # ----------------------------------------------------

        total_pages = (
            main_pages + comp_pages
        )

        total_failed = (
            main_failed + comp_failed
        )

        print("\n" + "=" * 70)
        print("CLOUD SCAN COMPLETE")
        print("=" * 70)

        print(f"Timestamp: {now}")
        print(
            f"{MAIN_PIN} unique variants: "
            f"{len(main_rows)}"
        )
        print(
            f"{COMPARISON_PIN} unique variants: "
            f"{len(comp_rows)}"
        )
        print(
            f"API pages: {total_pages}"
        )
        print(
            f"Failed searches: {total_failed}"
        )
        print(
            f"Historical stats: {len(stats)}"
        )
        print(
            f"NEW RARE DEALS: {len(deals)}"
        )

        if deals:

            print("\n*** NEW RARE DEALS ***")

            for d in deals[:20]:

                print(
                    f'RARE DEAL — '
                    f'{d["product_name"]} | '
                    f'{d["pack"]} | '
                    f'533006 ₹{d["current_price_533006"]:.0f} | '
                    f'typical ₹{d["historical_median"]:.0f} | '
                    f'500084 ₹{d["comparison_price"]:.0f} | '
                    f'score {d["score"]:.1f}'
                )

        else:
            print(
                "No new rare deal alerts this run."
            )

    finally:

        if c:
            c.close()


if __name__ == "__main__":
    main()
