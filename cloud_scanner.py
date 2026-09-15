import json
import os
import re
import time
from datetime import datetime, timezone

import httpx2
import psycopg


# ============================================================
# CONFIG
# ============================================================

SWIGGY_URL = "https://mcp.swiggy.com/im"

DB_HOST = "aws-0-ap-northeast-2.pooler.supabase.com"
DB_PORT = 6543
DB_NAME = "postgres"

MAIN_PIN = "533006"
COMPARISON_PIN = "500084"

DEALS_FILE = "rare_deals.json"
ALERT_LOG = "rare_deal_alerts.log"
TOP50_FILE = "top50_cheapest_533006.json"


# ============================================================
# SEARCH TERMS
# ============================================================

SEARCHES = [
    "snacks", "chips", "namkeen", "biscuits", "cookies", "chocolate",
    "candy", "sweets", "popcorn", "noodles", "pasta", "vermicelli",
    "soup", "sauce", "ketchup", "mayonnaise", "spread", "jam",
    "instant food", "ready to eat", "frozen food", "frozen snacks",
    "beverages", "soft drinks", "juice", "fruit juice", "energy drink",
    "water", "tea", "coffee", "green tea", "milk", "curd", "yogurt",
    "butter", "cheese", "paneer", "cream", "bread", "bun", "cake",
    "rusk", "croissant", "atta", "flour", "rice", "basmati rice",
    "dal", "lentils", "poha", "rava", "sooji", "oats", "cereal",
    "breakfast", "sugar", "salt", "jaggery", "honey", "oil", "ghee",
    "spices", "masala", "turmeric", "chilli powder", "cumin", "pepper",
    "garam masala", "pickle", "papad", "dry fruits", "nuts", "seeds",
    "personal care", "soap", "body wash", "shampoo", "conditioner",
    "hair oil", "face wash", "skin care", "moisturizer", "sunscreen",
    "toothpaste", "toothbrush", "mouthwash", "deodorant", "perfume",
    "shaving", "razor", "feminine care", "sanitary pads", "household",
    "cleaning", "floor cleaner", "toilet cleaner", "bathroom cleaner",
    "kitchen cleaner", "dishwash", "dish soap", "detergent",
    "washing powder", "air freshener", "tissue", "toilet tissue",
    "paper towels", "garbage bags", "aluminium foil", "storage bags",
    "baby care", "baby food", "baby diapers", "diapers", "baby wipes",
    "baby shampoo", "baby soap", "baby lotion", "pet care", "pet food",
    "dog food", "cat food", "pet treats", "pet litter", "meat",
    "chicken", "mutton", "fish", "seafood", "health care", "vitamins",
    "nutrition", "protein", "supplements", "first aid", "wellness",
    "medical devices", "electronics", "mobile accessories", "charger",
    "cable", "earphones", "batteries", "light bulbs", "led bulb",
    "stationery", "school supplies", "kitchen", "kitchen tools",
    "storage", "home utility", "clothing", "socks", "travel",
    "umbrella", "toys", "games", "gifts", "plants", "indoor plants",
    "outdoor plants", "flowering plants", "herbs", "succulents",
    "garden", "gardening", "plant pots", "planters", "gardening tools"
]


# ============================================================
# BASIC HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def normalize(value):
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def cross_key(product, variation):
    return (
        normalize(product.get("brand", "")),
        normalize(product.get("displayName", "")),
        normalize(variation.get("quantityDescription", ""))
    )


def product_key_string(key):
    return "|".join(key)


# ============================================================
# MCP RESPONSE HELPERS
# ============================================================

def parse_mcp_response(response):
    content_type = response.headers.get("content-type", "").lower()

    if "application/json" in content_type:
        return response.json()

    for line in response.text.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if not data:
                continue

            try:
                return json.loads(data)
            except json.JSONDecodeError:
                continue

    raise RuntimeError(
        "Could not parse Swiggy MCP response:\n"
        + response.text[:2000]
    )


def structured(result):
    return result.get(
        "result",
        {}
    ).get(
        "structuredContent",
        {}
    )


def products(result):
    return structured(result).get("products", [])


def addresses(result):
    return structured(result).get("addresses", [])


# ============================================================
# SWIGGY MCP
# ============================================================

def make_headers(token, session_id=None):
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }

    if session_id:
        headers["Mcp-Session-Id"] = session_id

    return headers


def initialize(client, token):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {
                "name": "instamart-cloud-deal-scanner",
                "version": "2.1.0",
            },
        },
    }

    response = client.post(
        SWIGGY_URL,
        headers=make_headers(token),
        json=payload,
        timeout=60,
    )

    if response.status_code in (401, 403):
        raise RuntimeError(
            "Swiggy token expired or unauthorized."
        )

    response.raise_for_status()

    parse_mcp_response(response)

    session_id = response.headers.get("mcp-session-id")

    headers = make_headers(token, session_id)

    client.post(
        SWIGGY_URL,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        timeout=60,
    )

    return session_id


def call_tool(client, token, session_id, request_id, name, arguments):
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
        },
    }

    headers = make_headers(token, session_id)

    for attempt in range(3):
        response = client.post(
            SWIGGY_URL,
            headers=headers,
            json=payload,
            timeout=60,
        )

        if response.status_code == 429:
            if attempt == 2:
                raise RuntimeError(
                    "Swiggy rate limit persisted after retries."
                )

            wait = 2 ** (attempt + 1)
            print(
                f"  Rate limited (429). Waiting {wait}s..."
            )
            time.sleep(wait)
            continue

        if response.status_code in (401, 403):
            raise RuntimeError(
                "Swiggy token expired or unauthorized."
            )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Swiggy HTTP {response.status_code}: "
                f"{response.text[:2000]}"
            )

        result = parse_mcp_response(response)

        return result

    raise RuntimeError("Unexpected Swiggy request failure.")


# ============================================================
# PRODUCT EXTRACTION
# ============================================================

def extract_rows(product_list, search_term):
    rows = {}

    for product in product_list:
        for variation in product.get("variations", []):
            price = variation.get("price", {}) or {}

            mrp = price.get("mrp")
            offer = price.get("offerPrice")

            if not isinstance(mrp, (int, float)):
                continue

            if not isinstance(offer, (int, float)):
                continue

            if mrp <= 0:
                continue

            if offer < 0 or offer > mrp:
                continue

            row = {
                "product_id": str(
                    product.get("productId", "")
                ),
                "sku_id": str(
                    variation.get("skuId", "")
                ),
                "spin_id": str(
                    variation.get("spinId", "")
                ),
                "variation_id": str(
                    variation.get("variationId", "")
                ),
                "brand": str(
                    product.get("brand", "")
                ),
                "name": str(
                    product.get("displayName", "Unknown")
                ),
                "pack": str(
                    variation.get("quantityDescription", "")
                ),
                "mrp": float(mrp),
                "offer": float(offer),
                "unit_price": str(
                    price.get("unitLevelPrice", "")
                ),
                "in_stock": bool(
                    variation.get(
                        "isInStockAndAvailable",
                        False
                    )
                ),
                "search_term": search_term,
            }

            key = cross_key(product, variation)

            # Keep the in-stock version when duplicates appear.
            if (
                key not in rows
                or (
                    row["in_stock"]
                    and not rows[key]["in_stock"]
                )
            ):
                rows[key] = row

    return rows


# ============================================================
# SUPABASE POSTGRESQL
# ============================================================

def db_connect():
    user = os.environ["SUPABASE_DB_USER"]
    password = os.environ["SUPABASE_DB_PASSWORD"]

    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=user,
        password=password,
        sslmode="require",
        connect_timeout=20,
    )


def save_main_history(conn, scan_time, rows):
    data = []

    for key, row in rows.items():
        data.append(
            (
                scan_time,
                product_key_string(key),
                row["product_id"],
                row["sku_id"],
                row["spin_id"],
                row["variation_id"],
                row["brand"],
                row["name"],
                row["pack"],
                row["mrp"],
                row["offer"],
                row["unit_price"],
                round(
                    (
                        row["mrp"]
                        - row["offer"]
                    )
                    / row["mrp"]
                    * 100,
                    2,
                ),
                row["in_stock"],
                row["search_term"],
            )
        )

    if not data:
        return

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO price_history (
                scan_time,
                product_key,
                product_id,
                sku_id,
                spin_id,
                variation_id,
                brand,
                product_name,
                pack,
                mrp,
                offer_price,
                unit_price,
                discount_pct,
                in_stock,
                search_term
            )
            VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s
            )
            """,
            data,
        )


def save_comparison_history(
    conn,
    scan_time,
    rows,
):
    data = []

    for key, row in rows.items():
        data.append(
            (
                scan_time,
                COMPARISON_PIN,
                product_key_string(key),
                row["brand"],
                row["name"],
                row["pack"],
                row["mrp"],
                row["offer"],
                row["unit_price"],
                row["in_stock"],
                row["search_term"],
            )
        )

    if not data:
        return

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO comparison_history (
                scan_time,
                comparison_pin,
                product_key,
                brand,
                product_name,
                pack,
                mrp,
                offer_price,
                unit_price,
                in_stock,
                search_term
            )
            VALUES (
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s
            )
            """,
            data,
        )


# ============================================================
# HISTORICAL PRICE ANALYSIS
# ============================================================

def percentile(values, fraction):
    values = sorted(values)

    if not values:
        return None

    if len(values) == 1:
        return values[0]

    position = (len(values) - 1) * fraction

    lower = int(position)
    upper = min(
        lower + 1,
        len(values) - 1
    )

    if lower == upper:
        return values[lower]

    return (
        values[lower]
        + (
            values[upper]
            - values[lower]
        )
        * (position - lower)
    )


def historical_stats(
    conn,
    row,
    before_time,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                offer_price,
                mrp
            FROM price_history
            WHERE
                lower(coalesce(brand, '')) = %s
                AND lower(coalesce(product_name, '')) = %s
                AND lower(coalesce(pack, '')) = %s
                AND scan_time < %s
                AND offer_price IS NOT NULL
                AND offer_price >= 0
                AND mrp IS NOT NULL
                AND mrp > 0
            ORDER BY scan_time
            """,
            (
                normalize(row["brand"]),
                normalize(row["name"]),
                normalize(row["pack"]),
                before_time,
            ),
        )

        history = cur.fetchall()

    values = []

    current_mrp = float(row["mrp"])

    for offer, mrp in history:
        offer = float(offer)
        mrp = float(mrp)

        # Reject old observations with a materially
        # different MRP. 5% tolerance protects against
        # normal data noise.
        if mrp > current_mrp * 1.05:
            continue

        # Never accept an invalid historical offer.
        if offer > mrp:
            continue

        values.append(offer)

    if len(values) < 3:
        return None

    return {
        "n": len(values),
        "median": sorted(values)[
            len(values) // 2
        ] if len(values) % 2 else (
            sorted(values)[
                len(values) // 2 - 1
            ]
            + sorted(values)[
                len(values) // 2
            ]
        ) / 2,
        "low": min(values),
        "p20": percentile(values, 0.20),
        "p10": percentile(values, 0.10),
    }


# ============================================================
# DUPLICATE ALERT PROTECTION
# ============================================================

def previous_alerted(
    conn,
    key,
    current_price,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_price
            FROM deal_alerts
            WHERE product_key = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (key,),
        )

        result = cur.fetchone()

    if not result:
        return False

    return current_price >= float(result[0])


# ============================================================
# TOP 50 CHEAPEST
# ============================================================

def top_50_cheapest(main_rows):
    """
    Return the 50 cheapest currently in-stock SKUs found in MAIN_PIN.
    This is intentionally independent of the rare-deal engine.
    """
    in_stock = [
        row for row in main_rows.values()
        if row["in_stock"]
        and isinstance(row.get("offer"), (int, float))
        and row["offer"] >= 0
    ]

    in_stock.sort(
        key=lambda row: (
            row["offer"],
            row["mrp"],
            normalize(row["brand"]),
            normalize(row["name"]),
            normalize(row["pack"]),
        )
    )

    results = []

    for rank, row in enumerate(in_stock[:50], 1):
        discount_pct = (
            (row["mrp"] - row["offer"]) / row["mrp"] * 100
            if row["mrp"] > 0
            else 0
        )

        results.append({
            "rank": rank,
            "product_name": row["name"],
            "pack": row["pack"],
            "current_price_533006": round(row["offer"], 2),
            "mrp": round(row["mrp"], 2),
            "discount_pct": round(discount_pct, 2),
            "unit_price": row["unit_price"],
            "brand": row["brand"],
            "in_stock": row["in_stock"],
        })

    return results


# ============================================================
# DEAL ENGINE
# ============================================================


def evaluate_deals(
    conn,
    scan_time,
    main_rows,
    comparison_rows,
):
    deals = []

    for key, row in main_rows.items():

        # Only alert for products currently in stock.
        if not row["in_stock"]:
            continue

        stats = historical_stats(
            conn,
            row,
            scan_time,
        )

        # Need at least 3 valid historical observations.
        if not stats:
            continue

        current = row["offer"]
        median = stats["median"]
        low = stats["low"]
        p20 = stats["p20"]

        # --------------------------------------------------------
        # LOW-PRICE NORMAL-DEAL FILTER
        #
        # Rare deals remain primarily historical-price driven.
        # This extra MRP gate only suppresses common low-priced
        # products unless their MRP discount is substantial.
        #
        # ₹10-₹20 -> at least 50% off MRP
        # ₹21-₹50 -> at least 40% off MRP
        # Above ₹50 -> no extra MRP gate
        # --------------------------------------------------------
        mrp_discount_pct = (
            (row["mrp"] - current) / row["mrp"] * 100
            if row["mrp"] > 0
            else 0
        )

        if current <= 20 and mrp_discount_pct < 50:
            continue

        if 20 < current <= 50 and mrp_discount_pct < 40:
            continue

        # Historical typical price must make sense
        # relative to today's MRP.
        if median > row["mrp"] * 1.05:
            continue

        if median <= 0:
            continue

        historical_drop = (
            median - current
        ) / median

        near_historical_low = (
            current <= low * 1.02
        )

        below_p20 = (
            current <= p20
        )

        strong_historical_deal = (
            historical_drop >= 0.25
            and below_p20
        )

        extreme_historical_deal = (
            historical_drop >= 0.35
            and near_historical_low
        )

        if not (
            strong_historical_deal
            or extreme_historical_deal
        ):
            continue

        comparison = comparison_rows.get(key)

        comparison_price = None

        if (
            comparison
            and comparison["in_stock"]
        ):
            comparison_price = comparison["offer"]

        # Cross-PIN support is mandatory.
        cross_pin_support = (
            comparison_price is not None
            and comparison_price >= current * 1.10
        )

        if not cross_pin_support:
            continue

        score = (
            50
            + min(
                30,
                historical_drop * 100
            )
            + (
                10
                if near_historical_low
                else 0
            )
            + (
                10
                if comparison_price >= current * 1.20
                else 0
            )
        )

        key_string = product_key_string(key)

        if previous_alerted(
            conn,
            key_string,
            current,
        ):
            continue

        reasons = [
            f"current ₹{current:.0f} "
            f"vs historical median ₹{median:.0f}",
            f"historical low ₹{low:.0f}",
            f"500084 ₹{comparison_price:.0f}",
        ]

        if near_historical_low:
            reasons.append(
                "near historical low"
            )

        if below_p20:
            reasons.append(
                "at/below historical 20th percentile"
            )

        deal = {
            "alert_time": scan_time.isoformat(),
            "product_key": key_string,
            "product_name": row["name"],
            "pack": row["pack"],
            "current_price_533006": current,
            "historical_median": round(
                median,
                2,
            ),
            "historical_low": round(
                low,
                2,
            ),
            "historical_p20": round(
                p20,
                2,
            ),
            "comparison_pin": COMPARISON_PIN,
            "comparison_price": comparison_price,
            "historical_observations": stats["n"],
            "score": round(score, 1),
            "reason": "; ".join(reasons),
        }

        deals.append(deal)

    # Save alerts in the same database transaction.
    if deals:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO deal_alerts (
                    alert_time,
                    product_key,
                    product_name,
                    pack,
                    current_price,
                    historical_median,
                    historical_low,
                    historical_p20,
                    comparison_price,
                    comparison_pin,
                    score,
                    reason
                )
                VALUES (
                    %s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s
                )
                """,
                [
                    (
                        scan_time,
                        deal["product_key"],
                        deal["product_name"],
                        deal["pack"],
                        deal[
                            "current_price_533006"
                        ],
                        deal[
                            "historical_median"
                        ],
                        deal[
                            "historical_low"
                        ],
                        deal[
                            "historical_p20"
                        ],
                        deal[
                            "comparison_price"
                        ],
                        deal[
                            "comparison_pin"
                        ],
                        deal["score"],
                        deal["reason"],
                    )
                    for deal in deals
                ],
            )

    return deals


# ============================================================
# MAIN SCAN
# ============================================================

def run_scan():
    token = os.environ.get("SWIGGY_TOKEN")

    if not token:
        raise RuntimeError(
            "SWIGGY_TOKEN GitHub secret is missing."
        )

    print("=" * 70)
    print(
        " INSTAMART CLOUD RARE-DEAL SCANNER"
    )
    print(
        " 533006 PRIMARY / 500084 SUPPORT"
    )
    print("=" * 70)

    print(
        f"Search terms: {len(SEARCHES)}"
    )

    print(
        "Database: Supabase PostgreSQL "
        "via Transaction Pooler"
    )

    print(
        "No cart/order changes."
    )

    scan_time = utc_now()

    # --------------------------------------------------------
    # SWIGGY
    # --------------------------------------------------------

    with httpx2.Client(
        timeout=60,
        follow_redirects=True,
    ) as client:

        print("")
        print(
            "Connecting to Swiggy MCP..."
        )

        session_id = initialize(
            client,
            token,
        )

        print(
            "Swiggy MCP: CONNECTED"
        )

        print("")
        print(
            "Getting saved addresses..."
        )

        address_result = call_tool(
            client,
            token,
            session_id,
            2,
            "get_addresses",
            {
                "page": 1,
                "pageSize": 10,
            },
        )

        saved_addresses = addresses(
            address_result
        )

        main_address = None

        for address in saved_addresses:
            if (
                str(
                    address.get(
                        "addressTag",
                        "",
                    )
                ).lower()
                == "home"
                and MAIN_PIN
                in str(address)
            ):
                main_address = address
                break

        if not main_address:
            for address in saved_addresses:
                if MAIN_PIN in str(address):
                    main_address = address
                    break

        comparison_address = None

        for address in saved_addresses:
            if COMPARISON_PIN in str(address):
                comparison_address = address
                break

        if not main_address:
            raise RuntimeError(
                f"{MAIN_PIN} saved address not found."
            )

        if not comparison_address:
            raise RuntimeError(
                f"{COMPARISON_PIN} comparison address not found."
            )

        main_address_id = main_address["id"]
        comparison_address_id = (
            comparison_address["id"]
        )

        print(
            f"{MAIN_PIN} main address: FOUND"
        )

        print(
            f"{COMPARISON_PIN} comparison address: FOUND"
        )

        # ----------------------------------------------------
        # MAIN PIN
        # ----------------------------------------------------

        main_rows = {}
        comparison_rows = {}

        pages = 0
        failed = 0

        print("")
        print(
            f"--- {MAIN_PIN} MAIN SCAN ---"
        )

        for index, search_term in enumerate(
            SEARCHES,
            1,
        ):
            print(
                f"[{MAIN_PIN} "
                f"{index:03}/{len(SEARCHES)}] "
                f"{search_term}"
            )

            try:
                result = call_tool(
                    client,
                    token,
                    session_id,
                    100 + index,
                    "search_products",
                    {
                        "addressId": main_address_id,
                        "query": search_term,
                        "offset": 0,
                    },
                )

                pages += 1

                found = extract_rows(
                    products(result),
                    search_term,
                )

                main_rows.update(found)

            except Exception as exc:
                failed += 1

                print(
                    "  FAILED:",
                    str(exc),
                )

            # Small delay to avoid hammering MCP.
            time.sleep(0.15)

        print("")
        print(
            f"{MAIN_PIN} unique variants: "
            f"{len(main_rows)}"
        )

        # ----------------------------------------------------
        # COMPARISON PIN
        # ----------------------------------------------------

        print("")
        print(
            f"--- {COMPARISON_PIN} "
            f"COMPARISON SCAN ---"
        )

        for index, search_term in enumerate(
            SEARCHES,
            1,
        ):
            print(
                f"[{COMPARISON_PIN} "
                f"{index:03}/{len(SEARCHES)}] "
                f"{search_term}"
            )

            try:
                result = call_tool(
                    client,
                    token,
                    session_id,
                    500 + index,
                    "search_products",
                    {
                        "addressId": comparison_address_id,
                        "query": search_term,
                        "offset": 0,
                    },
                )

                pages += 1

                found = extract_rows(
                    products(result),
                    search_term,
                )

                comparison_rows.update(
                    found
                )

            except Exception as exc:
                failed += 1

                print(
                    "  FAILED:",
                    str(exc),
                )

            time.sleep(0.15)

    # --------------------------------------------------------
    # DATABASE
    # --------------------------------------------------------

    print("")
    print(
        "Connecting to Supabase PostgreSQL..."
    )

    conn = db_connect()

    try:
        print(
            "PostgreSQL connection: OK"
        )

        print("")
        print(
            "Saving 533006 price history..."
        )

        save_main_history(
            conn,
            scan_time,
            main_rows,
        )

        print(
            "533006 history: SAVED"
        )

        print("")
        print(
            "Saving 500084 comparison history..."
        )

        save_comparison_history(
            conn,
            scan_time,
            comparison_rows,
        )

        print(
            "500084 history: SAVED"
        )

        print("")
        print(
            "Evaluating rare deals..."
        )

        deals = evaluate_deals(
            conn,
            scan_time,
            main_rows,
            comparison_rows,
        )

        conn.commit()

        print(
            "Database transaction: COMMITTED"
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    # Top 50 is a separate current-price view and is not affected
    # by rare-deal filtering.
    cheapest = top_50_cheapest(main_rows)

    # --------------------------------------------------------
    # OUTPUT
    # --------------------------------------------------------

    with open(
        DEALS_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            deals,
            file,
            indent=2,
            ensure_ascii=False,
        )

    with open(
        TOP50_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            cheapest,
            file,
            indent=2,
            ensure_ascii=False,
        )

    if deals:
        with open(
            ALERT_LOG,
            "a",
            encoding="utf-8",
        ) as file:

            for deal in deals:
                file.write(
                    f'{scan_time.isoformat()} | '
                    f'RARE DEAL | '
                    f'{deal["product_name"]} | '
                    f'{deal["pack"]} | '
                    f'533006 ₹'
                    f'{deal["current_price_533006"]:.0f} | '
                    f'median ₹'
                    f'{deal["historical_median"]:.0f} | '
                    f'500084 ₹'
                    f'{deal["comparison_price"]:.0f} | '
                    f'score '
                    f'{deal["score"]:.1f}\n'
                )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print("")
    print("=" * 70)
    print(" CLOUD SCAN COMPLETE")
    print("=" * 70)

    print(
        "Timestamp:",
        scan_time.isoformat(),
    )

    print(
        f"{MAIN_PIN} unique variants:",
        len(main_rows),
    )

    print(
        f"{COMPARISON_PIN} unique variants:",
        len(comparison_rows),
    )

    print(
        "API pages:",
        pages,
    )

    print(
        "Failed searches:",
        failed,
    )

    print(
        "Top 50 cheapest products:",
        len(cheapest),
    )

    print(
        "New RARE DEALS:",
        len(deals),
    )

    if deals:
        print("")
        print(
            "*** NEW RARE DEALS ***"
        )

        for deal in deals[:20]:
            print(
                f'RARE DEAL — '
                f'{deal["product_name"]} | '
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
    else:
        print("")
        print(
            "No new rare deal alerts this scan."
        )

    print("")
    print("TOP 50 CHEAPEST — FIRST 10")
    for item in cheapest[:10]:
        print(
            f'#{item["rank"]} '
            f'{item["product_name"]} | '
            f'{item["pack"]} | '
            f'533006 ₹{item["current_price_533006"]:.0f} | '
            f'MRP ₹{item["mrp"]:.0f} | '
            f'{item["discount_pct"]:.1f}% off'
        )

    print("=" * 70)


if __name__ == "__main__":
    run_scan()
