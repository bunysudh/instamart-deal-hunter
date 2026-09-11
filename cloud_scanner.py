import os
import psycopg


DB_HOST = "aws-0-ap-northeast-2.pooler.supabase.com"
DB_PORT = 6543
DB_NAME = "postgres"


def main():
    db_user = os.environ["SUPABASE_DB_USER"]
    db_password = os.environ["SUPABASE_DB_PASSWORD"]

    print("========================================")
    print("INSTAMART CLOUD SCANNER - DATABASE TEST")
    print("========================================")

    print("")
    print("Connecting to Supabase PostgreSQL...")
    print("Host:", DB_HOST)
    print("Port:", DB_PORT)
    print("Database:", DB_NAME)
    print("User: [hidden]")

    conn = psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=db_user,
        password=db_password,
        sslmode="require",
        connect_timeout=15,
    )

    try:
        with conn.cursor() as cur:

            print("")
            print("1. Testing database connection...")
            cur.execute("SELECT 1;")
            result = cur.fetchone()

            if result != (1,):
                raise RuntimeError(
                    f"Unexpected SELECT 1 result: {result}"
                )

            print("   DATABASE CONNECTION: OK")

            print("")
            print("2. Checking price_history table...")
            cur.execute(
                "SELECT COUNT(*) FROM price_history;"
            )
            count_before = cur.fetchone()[0]

            print(
                "   Existing price_history rows:",
                count_before
            )

            print("")
            print("3. Performing controlled write test...")

            cur.execute(
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
                    NOW(),
                    'CLOUD_TEST_ONLY',
                    'CLOUD_TEST',
                    'CLOUD_TEST',
                    'CLOUD_TEST',
                    'CLOUD_TEST',
                    'TEST',
                    'Cloud Scanner Database Test',
                    'TEST',
                    1,
                    1,
                    '1',
                    0,
                    TRUE,
                    'CLOUD_TEST'
                )
                RETURNING id;
                """
            )

            test_id = cur.fetchone()[0]

            print("   Test row inserted. ID:", test_id)

            print("")
            print("4. Reading test row back...")

            cur.execute(
                """
                SELECT
                    product_key,
                    product_name,
                    offer_price
                FROM price_history
                WHERE id = %s;
                """,
                (test_id,)
            )

            row = cur.fetchone()

            if row is None:
                raise RuntimeError(
                    "Test row could not be read back."
                )

            print("   Read-back result:", row)

            if row[0] != "CLOUD_TEST_ONLY":
                raise RuntimeError(
                    "Read-back product_key did not match."
                )

            print("   WRITE + READ: OK")

            print("")
            print("5. Rolling back test transaction...")

            conn.rollback()

            print("   Test data was NOT permanently saved.")

        print("")
        print("========================================")
        print("DATABASE TEST: SUCCESS")
        print("========================================")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()
