"""Demo seed data: one account, one owner login, three manifests covering
UPC-E/UPC-A/EAN-13/GS1-128 barcode variety, a handful of 5-digit collision
cases, and some alphanumeric 3PL-style SKUs. Used by serve.py on first run
and by tests that need a populated database.
"""

from __future__ import annotations

import auth
import barcode
import db
import manifest_ingest
import pricing
from sqlstore import SQL

DEMO_EMAIL = "owner@dockside-demo.test"
DEMO_PASSWORD = "dockside-demo-2026"

hash_password = auth.hash_password
verify_password = auth.verify_password


def _gtin_from_body11(body11: str) -> str:
    """11-digit UPC-A body (no check digit) -> full 12-digit UPC-A."""
    return body11 + barcode.compute_check_digit(body11)


def _upce_case012(n, s1, s2, s6, s3, s4, s5) -> str:
    """Build a real 8-digit UPC-E code for the S6-in-{0,1,2} compression case.

    A UPC-E code is N S1 S2 S3 S4 S5 S6 C -- S6 last, immediately before the
    check digit. This used to assemble the digits in the expansion's order
    (N S1 S2 S6 S3 S4 S5), which put S6 fourth, so the "genuine UPC-E
    compressed code" the demo advertises was not one: 04315674 expands to
    043156000074 rather than the intended 043100005674, and the two lines
    documented as demonstrating GTIN-tier collapsing shared no keys at all.

    Rather than transcribe the digit order a second time, this now builds
    the UPC-E body directly and rounds it through barcode.py's own
    expansion, so the seed and the engine cannot disagree about what this
    code means.
    """
    upce_body = f"{n}{s1}{s2}{s3}{s4}{s5}{s6}"
    upca_body = barcode.upce_body_to_upca_body(upce_body)
    return upce_body + barcode.compute_check_digit(upca_body)


def _electronics_manifest_rows() -> list[dict]:
    rows = []
    # A batch of "clean" EAN-13 codes, computed with a real check digit.
    for i, (mfr, prod) in enumerate(
        [("612345", "00101"), ("612345", "00102"), ("745123", "88231"), ("998877", "00099")]
    ):
        # mfr here is 6 digits (system digit included), unlike the 5-digit
        # values in _grocery_manifest_rows -- so it must NOT get the extra
        # "0" that function prepends. With it the body was 12 digits and
        # _gtin_from_body11 returned 13, which then overran AI 01's
        # fixed 14-digit field below and produced a 15-digit "GTIN".
        body11 = mfr + prod  # 6-digit system+manufacturer + 5-digit product
        upca = _gtin_from_body11(body11)
        rows.append(
            {
                "line_no": i + 1,
                "sku": f"ELEC-{mfr}-{prod}",
                "description": f"Electronics item {mfr}-{prod}",
                "qty_expected": 1,
                "raw_barcode": upca,
            }
        )
    # Same physical products, but exported on this manifest as GS1-128
    # element strings with lot/serial metadata -- must match the plain
    # EAN-13/UPC-A lines above on GTIN if reused, but here they're new SKUs.
    # Same 6-digit-mfr convention as above: no extra "0". AI 01 is a
    # fixed-length 14-digit field, so "00" + a 12-digit UPC-A is the only
    # thing that fits it.
    gs1_gtin = _gtin_from_body11("500001" + "00050")
    rows.append(
        {
            "line_no": 5,
            "sku": "ELEC-500001-00050",
            "description": "Serialized power bank",
            "qty_expected": 1,
            "raw_barcode": f"(01)00{gs1_gtin}(10)LOT7A(21)SN000123",
        }
    )
    rows.append(
        {
            "line_no": 6,
            "sku": "ELEC-500001-00050",
            "description": "Serialized power bank (unserialized twin on manifest)",
            "qty_expected": 3,
            "raw_barcode": "00" + gs1_gtin,
        }
    )
    # A genuine UPC-E compressed code (case S6=1) alongside its UPC-A form
    # on a *different* line, to demonstrate GTIN-tier collapsing without
    # being the same manifest line.
    #
    # The companion line is the half that was missing: the comment described
    # a pair, the code emitted only the compressed member of it, and the
    # compressed member was not even a valid UPC-E (see _upce_case012). Both
    # lines are here now, and tests/test_upce_vectors.py asserts they
    # actually collapse to the same GTIN-14 -- which is the whole point of
    # the demo and was previously true of nothing in it.
    upce = _upce_case012(0, 4, 3, 1, 5, 6, 7)
    rows.append(
        {
            "line_no": 7,
            "sku": "ELEC-COMPRESSED-1",
            "description": "Small accessory, printed as UPC-E",
            "qty_expected": 2,
            "raw_barcode": upce,
        }
    )
    rows.append(
        {
            "line_no": 8,
            "sku": "ELEC-COMPRESSED-1-UPCA",
            "description": "Small accessory, same item printed as full UPC-A",
            "qty_expected": 1,
            "raw_barcode": barcode.upce_to_upca(upce),
        }
    )
    return rows


def _apparel_manifest_rows() -> list[dict]:
    rows = []
    # Alphanumeric 3PL-style SKUs (Code 39 / Code 128 alpha payloads).
    for i, sku in enumerate(["APL-BLU-M-001", "APL-BLU-L-001", "APL-RED-S-002", "3PL/ALT-9944"]):
        rows.append(
            {
                "line_no": i + 1,
                "sku": sku,
                "description": f"Apparel {sku}",
                "qty_expected": 1,
                "raw_barcode": sku,
            }
        )
    # Five-digit internal codes that deliberately collide with each other
    # under loose (digits-stripped / suffix) tiers -- these should trigger
    # the ingest-time collision warning and get loose matching disabled
    # for exactly these keys.
    for i, suffix in enumerate(["40001", "40001", "40001", "40002", "40002"]):
        rows.append(
            {
                "line_no": 5 + i,
                "sku": f"APL-SHORTCODE-{i}",
                "description": "Legacy 5-digit internal code",
                "qty_expected": 1,
                "raw_barcode": suffix,
            }
        )
    return rows


def _grocery_manifest_rows() -> list[dict]:
    rows = []
    # True UPC-A codes: 1 system digit + 5-digit manufacturer + 5-digit
    # product = 11-digit body, + check digit = 12 total.
    for i, (mfr, prod) in enumerate(
        [("11220", "30011"), ("11220", "30012"), ("33004", "77001"), ("44556", "12309")]
    ):
        upca = _gtin_from_body11("0" + mfr + prod)
        rows.append(
            {
                "line_no": i + 1,
                "sku": f"GRO-{mfr}-{prod}",
                "description": f"Grocery item {mfr}-{prod}",
                "qty_expected": 4,
                "raw_barcode": upca,
            }
        )
    # This manifest export stripped the check digit (11-digit body only) --
    # some exports do. A worker's phone camera still reads the *full*
    # printed barcode (12 digits, check digit included). Tier 4
    # (body-with-check-removed) exists for exactly this mismatch.
    body11 = "0" + "77889" + "90001"
    rows.append(
        {
            "line_no": 5,
            "sku": "GRO-77889-90001",
            "description": "Manifest export missing check digit",
            "qty_expected": 2,
            "raw_barcode": body11,
        }
    )
    return rows


def seed_demo_account(conn) -> dict:
    account_id = db.create_account(
        conn,
        name="Dockside Supply Co. (demo)",
        price_per_catch_cents=pricing.DEFAULT_PRICE_PER_CATCH_CENTS,
        free_allowance=pricing.DEFAULT_FREE_ALLOWANCE,
        loose_match_enabled=True,
        loose_suffix_len=8,
        worker_self_resolve=False,
    )
    user_id = db.create_user(
        conn, account_id, DEMO_EMAIL, hash_password(DEMO_PASSWORD), role="owner"
    )

    manifest_ids = []
    for ref, rows_fn, filename in [
        ("ELEC-2026-07-25", _electronics_manifest_rows, "electronics_restock.csv"),
        ("APL-2026-07-25", _apparel_manifest_rows, "apparel_pick.csv"),
        ("GRO-2026-07-25", _grocery_manifest_rows, "grocery_ship.csv"),
    ]:
        manifest_id, report = manifest_ingest.commit_manifest(
            conn,
            account_id,
            ref,
            filename,
            rows_fn(),
            loose_match_enabled=True,
            loose_suffix_len=8,
        )
        manifest_ids.append((manifest_id, report))

    return {
        "account_id": account_id,
        "user_id": user_id,
        "email": DEMO_EMAIL,
        "password": DEMO_PASSWORD,
        "manifest_ids": manifest_ids,
    }


def is_seeded(conn) -> bool:
    row = db.query_one(conn, SQL["meta.any_account_exists"])
    return row is not None


def ensure_seeded(conn) -> dict | None:
    if is_seeded(conn):
        return None
    return seed_demo_account(conn)
