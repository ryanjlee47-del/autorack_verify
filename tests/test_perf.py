"""Matching performance budget.

Speed is the product: 200 orders/day/worker means matching runs thousands
of times per shift. The scan-path budget is a Map lookup, not a database
query -- this test builds a realistic 20,000-line index and asserts a
single match() call stays comfortably under 1ms even at that scale, so a
regression that turns the O(1) tier lookups into an O(n) scan gets caught
here instead of on a warehouse floor.
"""

import random
import time

from barcode import MatchIndex

random.seed(1234567)


def _synthetic_barcode(i: int) -> str:
    kind = i % 4
    if kind == 0:
        return str(10**12 + i).zfill(13)  # EAN-13-shaped
    if kind == 1:
        return str(10**11 + i).zfill(12)  # UPC-A-shaped
    if kind == 2:
        return f"SKU-{i:07d}-ALT"  # alphanumeric
    return f"(01)000{i:011d}(10)LOT{i % 97}"  # GS1-128-shaped


def _build_index(n: int) -> MatchIndex:
    idx = MatchIndex(loose_match_enabled=True, suffix_len=8)
    for i in range(n):
        idx.add_line(i, _synthetic_barcode(i))
    return idx


def test_match_stays_under_1ms_per_lookup_against_20k_lines():
    n = 20_000
    idx = _build_index(n)

    sample_ids = random.sample(range(n), 500)
    payloads = [_synthetic_barcode(i) for i in sample_ids]

    # Warm up (avoid measuring first-call interpreter/import overhead).
    for p in payloads[:20]:
        idx.match(p)

    start = time.perf_counter()
    for p in payloads:
        result = idx.match(p)
        assert result.is_resolved
    elapsed = time.perf_counter() - start

    per_lookup_ms = (elapsed / len(payloads)) * 1000
    assert per_lookup_ms < 1.0, f"match() averaged {per_lookup_ms:.4f}ms/lookup, budget is 1ms"


def test_match_stays_fast_for_unresolved_lookups_too():
    n = 20_000
    idx = _build_index(n)
    unknown_payloads = [f"NOT-ON-MANIFEST-{i}" for i in range(500)]

    start = time.perf_counter()
    for p in unknown_payloads:
        result = idx.match(p)
        assert not result.is_resolved
    elapsed = time.perf_counter() - start

    per_lookup_ms = (elapsed / len(unknown_payloads)) * 1000
    assert per_lookup_ms < 1.0, (
        f"match() averaged {per_lookup_ms:.4f}ms/lookup on misses, budget is 1ms"
    )
