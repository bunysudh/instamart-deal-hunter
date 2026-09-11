import os
import json
import time
import socket
import urllib.request
import urllib.error
import urllib.parse
import importlib.util
from datetime import datetime, timezone, timedelta


# ============================================================
# CONFIG
# ============================================================

SWIGGY_TOKEN = os.environ["SWIGGY_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

MAIN_PIN = "533006"
COMPARISON_PIN = "500084"

SCANNER_FILE = "instamart_final_533006_500084_v2.py"
DEALS_FILE = "rare_deals.json"


# ============================================================
# LOAD THE EXISTING INSTAMART SCANNER
# ============================================================

spec = importlib.util.spec_from_file_location(
    "instamart_scanner",
    SCANNER_FILE,
)

scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)

scanner.TOKEN = SWIGGY_TOKEN


# ============================================================
# SUPABASE HEADERS
# ============================================================

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": "Bearer " + SUPABASE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}


# ============================================================
# SUPABASE DIRECT CONNECTION
#
# IMPORTANT:
# GitHub runners can have HTTP(S)_PROXY environment variables.
# urllib normally obeys those variables.
#
# We deliberately create an opener with NO proxy so Supabase
# is contacted directly.
# ============================================================

DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)


def supabase_host():
    parsed = urllib.parse.urlparse(SUPABASE_URL)
    host = parsed.hostname

    if not host:
        raise RuntimeError(
            f"Could not determine Supabase hostname from: {SUPABASE_URL}"
        )

    return host


def supabase_dns_check():
    host = supabase_host()

    print("Supabase URL:", SUPABASE_URL)
    print("Supabase host:", host)

    try:
        ip = socket.gethostbyname(host)
        print("Supabase IPv4:", ip)
        return ip
    except Exception as e:
        raise RuntimeError(
            f"Supabase DNS resolution failed for {host}: {e}"
        )


def supabase_post(url, payload):
    """
    POST to Supabase using a direct connection with proxies disabled.
    """

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers=SB_HEADERS,
    )

    last_error = None

    for attempt in range(3):
        try:
            print(
                f"Connecting to Supabase "
                f"(direct connection, attempt {attempt + 1}/3)..."
            )

            with DIRECT_OPENER.open(
                request,
                timeout=120,
            ) as response:

                body = response.read().decode(
                    "utf-8",
                    errors="replace",
                )

                return response.status, body

        except urllib.error.HTTPError as e:

            body = e.read().decode(
                "utf-8",
                errors="replace",
            )

            return e.code, body

        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as e:

            last_error = e

            print(
                f"Supabase connection attempt "
                f"{attempt + 1} failed: {e}"
            )

            if attempt < 2:
                wait = 2 ** attempt
                print(f"Retrying in {wait} seconds...")
                time.sleep(wait)

    raise RuntimeError(
        "Could not connect to Supabase after 3 attempts: "
        f"{last_error}"
    )


# ============================================================
# SUPABASE RPC
# ============================================================

def supabase_rpc(function_name, payload):

    url = (
        f"{SUPABASE_URL}/rest/v1/rpc/"
        f"{function_name}"
    )

    status, body = supabase_post(
        url,
        payload,
    )

    if status >= 400:
        raise RuntimeError(
            f"Supabase RPC {function_name} failed: "
            f"HTTP {status}: {body[:2000]}"
        )

    if not body.strip():
        return []

    try:
        return json.loads(body)
    except Exception as e:
        raise RuntimeError(
            f"Supabase RPC {function_name} returned "
            f"invalid JSON: {e}\n"
            f"Response: {body[:2000]}"
        )


# ============================================================
# SUPABASE INSERT
# ============================================================

def supabase_insert(table, rows):

    if not rows:
        return

    url = (
        f"{SUPABASE_URL}/rest/v1/"
        f"{table}"
    )

    chunk_size = 500

    for start in range(0, len(rows), chunk_size):

        chunk = rows[start:start + chunk_size]

        status, body = supabase_post(
            url,
            chunk,
        )

        if status >= 400:
            raise RuntimeError(
                f"Supabase insert into {table} failed: "
                f"HTTP {status}: {body[:2000]}"
            )

        print(
            f"Supabase: inserted "
            f"{len(chunk)} rows into {table}"
        )


# ============================================================
# SUPABASE CONNECTION TEST
# ============================================================

def test_supabase():

    print()
    print("=" * 70)
    print("TESTING DIRECT SUPABASE CONNECTION")
    print("=" * 70)

    supabase_dns_check()

    # Use a harmless RPC call that already exists.
    # Empty product list should simply return no rows.
    result = supabase_rpc(
        "get_latest_main_prices",
        {
            "p_product_keys": []
        },
    )

    print(
        "Supabase direct connection: SUCCESS"
    )

    print(
        "Supabase RPC response rows:",
        len(result),
    )

    print("=" * 70)
    print()


# ============================================================
# ADDRESS HELPERS
# ============================================================

def get_addresses(client, session_id):

    _, result = scanner.call(
        client,
        session_id,
        2,
        "get_addresses",
        {
            "page": 1,
            "pageSize": 10,
        },
    )

    return scanner.addresses(result)


def find_addresses(address_list):

    main_addr = None
    comparison_addr = None

    for address in address_list:

        text = str(address)

        if (
            MAIN_PIN in text
            and str(address.get("addressTag", "")).lower()
            == "home"
        ):
            main_addr = address
            break

    if not main_addr:

        for address in address_list:

            if MAIN_PIN in str(address):
                main_addr = address
                break

    for address in address_list:

        if COMPARISON_PIN in str(address):
            comparison_addr = address
            break

    if not main_addr:
        raise RuntimeError(
            f"{MAIN_PIN} address not found."
        )

    if not comparison_addr:
        raise RuntimeError(
            f"{COMPARISON_PIN} comparison address not found."
        )

    return main_addr, comparison_addr


# ============================================================
# SCAN ONE PIN
# ============================================================

def scan_pin(
    client,
    session_id,
    address_id,
    pin,
    request_start,
):

    rows = {}
    pages = 0
    failed = 0

    searches = scanner.SEARCHES

    print()
    print("=" * 70)
    print(f"--- {pin} SCAN ---")
    print("=" * 70)

    for i, query in enumerate(
        searches,
        1,
    ):

        print(
            f"[{pin} {i:03}/{len(searches)}] "
            f"{query}"
        )

        try:

            _, result = scanner.call(
                client,
                session_id,
                request_start + i,
                "search_products",
                {
                    "addressId": address_id,
                    "query": query,
                    "offset": 0,
                },
            )

            pages += 1

            products = scanner.products(
                result
            )

            extracted = scanner.extract_rows(
                products,
                query,
            )

            rows.update(extracted)

        except Exception as e:

            failed += 1

            print(
                "  FAILED:",
                e,
            )

        # Keep the same conservative request pacing.
        time.sleep(0.15)

    print()
    print(
        f"{pin} scan finished: "
        f"{len(rows)} unique variants, "
        f"{pages} API pages, "
        f"{failed} failed searches"
    )

    return rows, pages, failed


# ============================================================
# HISTORY HELPERS
# ============================================================

def build_main_items(main_rows):

    items = []

    for key, row in main_rows.items():

        items.append(
            {
                "product_key": "|".join(key),
                "current_mrp": row["mrp"],
            }
        )

    return items


def get_history_stats(
    main_rows,
    before_time,
):

    items = build_main_items(
        main_rows
    )

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
        row["product_key"]: row
        for row in rows
    }


def get_latest_main_prices(
    product_keys,
):

    if not product_keys:
        return {}

    rows = supabase_rpc(
        "get_latest_main_prices",
        {
            "p_product_keys": product_keys,
        },
    )

    return {
        row["product_key"]: row
        for row in rows
    }


def get_latest_comparison_prices(
    product_keys,
):

    if not product_keys:
        return {}

    rows = supabase_rpc(
        "get_latest_comparison_prices",
        {
            "p_product_keys": product_keys,
            "p_pin": COMPARISON_PIN,
        },
    )

    return {
        row["product_key"]: row
        for row in rows
    }


def get_latest_alerts(
    product_keys,
):

    if not product_keys:
        return {}

    rows = supabase_rpc(
        "get_latest_alerts",
        {
            "p_product_keys": product_keys,
        },
    )

    return {
        row["product_key"]: row
        for row in rows
    }


# ============================================================
# HISTORY COMPRESSION
#
# We do NOT save an identical hourly observation every hour.
#
# Save:
#   - first observation
#   - price/MRP changes
#   - at least one observation every 24 hours
#
# This keeps the free Supabase database small.
# ============================================================

def should_store(
    key,
    row,
    latest_main,
    now_dt,
):

    previous = latest_main.get(key)

    if not previous:
        return True

    previous_price = previous.get(
        "offer_price"
    )

    previous_mrp = previous.get(
        "mrp"
    )

    current_price = row["offer"]
    current_mrp = row["mrp"]

    if previous_price is None:
        return True

    if abs(
        float(previous_price)
        - float(current_price)
    ) >= 0.01:
        return True

    if previous_mrp is None:
        return True

    if abs(
        float(previous_mrp)
        - float(current_mrp)
    ) >= 0.01:
        return True

    previous_time = previous.get(
        "scan_time"
    )

    if previous_time:

        try:
            previous_dt = datetime.fromisoformat(
                previous_time.replace(
                    "Z",
                    "+00:00",
                )
            )

            if (
                now_dt - previous_dt
                >= timedelta(hours=24)
            ):
                return True

        except Exception:
            return True

    return False


# ============================================================
# SAVE MAIN HISTORY
# ============================================================

def save_main_history(
    main_rows,
    latest_main,
    scan_time,
):

    now_dt = datetime.now(
        timezone.utc
    )

    rows = []

    for key, row in main_rows.items():

        product_key = "|".join(key)

        if not should_store(
            product_key,
            row,
            latest_main,
            now_dt,
        ):
            continue

        discount = (
            (row["mrp"] - row["offer"])
            / row["mrp"]
            * 100
        )

        rows.append(
            {
                "scan_time": scan_time,
                "product_key": product_key,
                "product_id": row["product_id"],
                "sku_id": row["sku_id"],
                "spin_id": row["spin_id"],
                "variation_id": row["variation_id"],
                "brand": row["brand"],
                "product_name": row["name"],
                "pack": row["pack"],
                "mrp": row["mrp"],
                "offer_price": row["offer"],
                "unit_price": row["unit_price"],
                "discount_pct": round(
                    discount,
                    2,
                ),
                "in_stock": row["in_stock"],
                "search_term": row["search_term"],
            }
        )

    if rows:

        supabase_insert(
            "price_history",
            rows,
        )

    print(
        "Main history rows saved:",
        len(rows),
    )


# ============================================================
# SAVE COMPARISON HISTORY
# ============================================================

def save_comparison_history(
    comp_rows,
    scan_time,
):

    rows = []

    for key, row in comp_rows.items():

        rows.append(
            {
                "scan_time": scan_time,
                "comparison_pin": COMPARISON_PIN,
                "product_key": "|".join(key),
                "brand": row["brand"],
                "product_name": row["name"],
                "pack": row["pack"],
                "mrp": row["mrp"],
                "offer_price": row["offer"],
                "unit_price": row["unit_price"],
                "in_stock": row["in_stock"],
                "search_term": row["search_term"],
            }
        )

    if rows:

        supabase_insert(
            "comparison_history",
            rows,
        )

    print(
        "Comparison history rows saved:",
        len(rows),
    )


# ============================================================
# DEAL EVALUATION
# ============================================================

def evaluate_deals(
    main_rows,
    comp_rows,
    history_stats,
    previous_alerts,
):

    deals = []

    for key, row in main_rows.items():

        if not row["in_stock"]:
            continue

        product_key = "|".join(key)

        stats = history_stats.get(
            product_key
        )

        if not stats:
            continue

        current = float(
            row["offer"]
        )

        median = float(
            stats["median"]
        )

        historical_low = float(
            stats["low"]
        )

        p20 = float(
            stats["p20"]
        )

        observations = int(
            stats["n"]
        )

        # ----------------------------------------------------
        # MRP SANITY CHECK
        # ----------------------------------------------------

        if median > float(
            row["mrp"]
        ) * 1.05:
            continue

        # ----------------------------------------------------
        # PRIMARY HISTORICAL PRICE TEST
        # ----------------------------------------------------

        if median <= 0:
            continue

        historical_drop = (
            median - current
        ) / median

        at_historical_low = (
            current
            <= historical_low * 1.02
        )

        below_p20 = (
            current <= p20
        )

        strong_historical_deal = (
            historical_drop >= 0.25
            and below_p20
        )

        very_strong_low = (
            historical_drop >= 0.35
            and at_historical_low
        )

        if not (
            strong_historical_deal
            or very_strong_low
        ):
            continue

        # ----------------------------------------------------
        # CROSS-PIN SUPPORT
        # ----------------------------------------------------

        comparison = comp_rows.get(
            key
        )

        comparison_price = None

        if (
            comparison
            and comparison["in_stock"]
        ):
            comparison_price = float(
                comparison["offer"]
            )

        if comparison_price is None:
            continue

        cross_support = (
            comparison_price
            >= current * 1.10
        )

        if not cross_support:
            continue

        # ----------------------------------------------------
        # PREVIOUS ALERT SUPPRESSION
        # ----------------------------------------------------

        previous = previous_alerts.get(
            product_key
        )

        if previous:

            previous_price = (
                previous.get(
                    "current_price"
                )
            )

            if (
                previous_price is not None
                and current
                >= float(previous_price)
            ):
                continue

        # ----------------------------------------------------
        # SCORE
        # ----------------------------------------------------

        score = (
            50
            + min(
                30,
                historical_drop * 100,
            )
        )

        if at_historical_low:
            score += 10

        if (
            comparison_price
            >= current * 1.20
        ):
            score += 10

        # ----------------------------------------------------
        # REASON
        # ----------------------------------------------------

        reasons = [
            (
                f"current ₹{current:.0f} "
                f"vs historical median "
                f"₹{median:.0f}"
            ),
            (
                f"historical low "
                f"₹{historical_low:.0f}"
            ),
            (
                f"{COMPARISON_PIN} "
                f"₹{comparison_price:.0f}"
            ),
        ]

        if at_historical_low:
            reasons.append(
                "near historical low"
            )

        if below_p20:
            reasons.append(
                "at/below historical 20th percentile"
            )

        deal = {
            "alert_time": datetime.now(
                timezone.utc
            ).isoformat(),

            "product_key": product_key,

            "product_name": row["name"],

            "pack": row["pack"],

            "current_price_533006": current,

            "historical_median": round(
                median,
                2,
            ),

            "historical_low": round(
                historical_low,
                2,
            ),

            "historical_p20": round(
                p20,
                2,
            ),

            "comparison_pin":
                COMPARISON_PIN,

            "comparison_price":
                comparison_price,

            "historical_observations":
                observations,

            "score": round(
                score,
                1,
            ),

            "reason":
                "; ".join(reasons),
        }

        deals.append(
            deal
        )

    return deals


# ============================================================
# SAVE DEAL ALERTS
# ============================================================

def save_deals(deals):

    if not deals:
        return

    rows = []

    for deal in deals:

        rows.append(
            {
                "alert_time":
                    deal["alert_time"],

                "product_key":
                    deal["product_key"],

                "product_name":
                    deal["product_name"],

                "pack":
                    deal["pack"],

                "current_price":
                    deal["current_price_533006"],

                "historical_median":
                    deal["historical_median"],

                "historical_low":
                    deal["historical_low"],

                "historical_p20":
                    deal["historical_p20"],

                "comparison_price":
                    deal["comparison_price"],

                "comparison_pin":
                    deal["comparison_pin"],

                "score":
                    deal["score"],

                "reason":
                    deal["reason"],
            }
        )

    supabase_insert(
        "deal_alerts",
        rows,
    )


# ============================================================
# MAIN CLOUD SCAN
# ============================================================

def main():

    print("=" * 70)
    print(
        " INSTAMART CLOUD RARE-DEAL SCANNER"
    )
    print(
        " 533006 vs 500084"
    )
    print("=" * 70)

    print(
        "Primary: 533006 historical price"
    )

    print(
        "Supporting: 500084 current price"
    )

    print(
        "Alerts require historical rarity "
        "+ cross-PIN support "
        "+ MRP sanity checks."
    )

    print(
        "No cart/order changes."
    )

    print("=" * 70)
    print()

    # --------------------------------------------------------
    # FIRST: TEST SUPABASE DIRECT CONNECTION
    # --------------------------------------------------------

    test_supabase()

    # --------------------------------------------------------
    # INITIALIZE SWIGGY MCP SESSION
    # --------------------------------------------------------

    print(
        "Initializing Swiggy MCP session..."
    )

    client, session_id = (
        scanner.init_session()
    )

    try:

        # ----------------------------------------------------
        # GET SAVED ADDRESSES
        # ----------------------------------------------------

        address_list = get_addresses(
            client,
            session_id,
        )

        main_address, comparison_address = (
            find_addresses(
                address_list
            )
        )

        print(
            "533006 main address found."
        )

        print(
            "500084 comparison address found."
        )

        # ----------------------------------------------------
        # SCAN MAIN PIN
        # ----------------------------------------------------

        started = time.time()

        main_rows, main_pages, main_failed = (
            scan_pin(
                client,
                session_id,
                main_address["id"],
                MAIN_PIN,
                10,
            )
        )

        # ----------------------------------------------------
        # SCAN COMPARISON PIN
        # ----------------------------------------------------

        comp_rows, comp_pages, comp_failed = (
            scan_pin(
                client,
                session_id,
                comparison_address["id"],
                COMPARISON_PIN,
                500,
            )
        )

        scan_time = datetime.now(
            timezone.utc
        ).isoformat()

        # ----------------------------------------------------
        # GET CURRENT DATABASE STATE
        # ----------------------------------------------------

        print()
        print(
            "Getting latest stored prices..."
        )

        product_keys = [
            "|".join(key)
            for key in main_rows.keys()
        ]

        latest_main = (
            get_latest_main_prices(
                product_keys
            )
        )

        # ----------------------------------------------------
        # SAVE MAIN HISTORY
        # ----------------------------------------------------

        print()
        print(
            "Saving 533006 history..."
        )

        save_main_history(
            main_rows,
            latest_main,
            scan_time,
        )

        # ----------------------------------------------------
        # SAVE COMPARISON HISTORY
        # ----------------------------------------------------

        print()
        print(
            "Saving 500084 comparison history..."
        )

        save_comparison_history(
            comp_rows,
            scan_time,
        )

        # ----------------------------------------------------
        # HISTORICAL STATISTICS
        # ----------------------------------------------------

        print()
        print(
            "Calculating historical statistics..."
        )

        history_stats = (
            get_history_stats(
                main_rows,
                scan_time,
            )
        )

        print(
            "Products with usable history:",
            len(history_stats),
        )

        # ----------------------------------------------------
        # PREVIOUS ALERTS
        # ----------------------------------------------------

        print()
        print(
            "Checking previous alerts..."
        )

        previous_alerts = (
            get_latest_alerts(
                product_keys
            )
        )

        # ----------------------------------------------------
        # DEAL EVALUATION
        # ----------------------------------------------------

        print()
        print(
            "Evaluating rare deals..."
        )

        deals = evaluate_deals(
            main_rows,
            comp_rows,
            history_stats,
            previous_alerts,
        )

        # ----------------------------------------------------
        # SAVE DEALS TO JSON
        # ----------------------------------------------------

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
        # SAVE ALERTS TO SUPABASE
        # ----------------------------------------------------

        if deals:

            print()
            print(
                "New rare deals found:",
                len(deals),
            )

            save_deals(
                deals
            )

        else:

            print()
            print(
                "No new rare deal alerts."
            )

        # ----------------------------------------------------
        # FINAL REPORT
        # ----------------------------------------------------

        elapsed = (
            time.time()
            - started
        )

        print()
        print("=" * 70)
        print(
            "SCAN COMPLETE"
        )
        print("=" * 70)

        print(
            f"533006 unique variants: "
            f"{len(main_rows)}"
        )

        print(
            f"500084 unique variants: "
            f"{len(comp_rows)}"
        )

        print(
            f"API pages: "
            f"{main_pages + comp_pages}"
        )

        print(
            f"Failed searches: "
            f"{main_failed + comp_failed}"
        )

        print(
            f"Products with history: "
            f"{len(history_stats)}"
        )

        print(
            f"New RARE DEALS: "
            f"{len(deals)}"
        )

        print(
            f"Runtime: "
            f"{elapsed / 60:.1f} minutes"
        )

        print(
            f"Results file: "
            f"{DEALS_FILE}"
        )

        if deals:

            print()
            print(
                "*** NEW RARE DEALS ***"
            )

            for deal in deals[:20]:

                print(
                    f'RARE DEAL — '
                    f'{deal["product_name"]} '
                    f'{deal["pack"]} | '
                    f'533006 ₹'
                    f'{deal["current_price_533006"]:.0f} | '
                    f'typical ₹'
                    f'{deal["historical_median"]:.0f} | '
                    f'500084 ₹'
                    f'{deal["comparison_price"]:.0f} | '
                    f'score '
                    f'{deal["score"]:.1f}'
                )

        print(
            "=" * 70
        )

    finally:

        client.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
