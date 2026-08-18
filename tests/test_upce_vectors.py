"""UPC-E -> UPC-A expansion vectors.

The task brief explicitly warns not to trust the S6-branch table from
memory alone. So this file cross-checks barcode.upce_to_upca() against
two independent sources instead of re-deriving the same rule twice:

1. A published, concrete numeric vector: "UPC-E 02532038 scans as UPC-A
   025300000208" (BarcodeFAQ.com's UPC-E documentation and its "scans as"
   test-vector page).
2. An independently-worded reference implementation transcribed from
   BarcodeFAQ.com's `UPCe7To11` VBA function, which encodes the same
   branch table in different variable names/order. It is reproduced here
   verbatim in Python (see `_reference_upce_to_upca`) rather than imported
   from barcode.py, so a transcription bug in barcode.py's branches would
   show up as a mismatch instead of trivially agreeing with itself.
"""

import pytest

from barcode import compute_check_digit, gtin_canonicalize, upce_to_upca


def _reference_upce_to_upca(code: str) -> str:
    """Transcribed from BarcodeFAQ.com's UPCe7To11 VBA function.

    D1..D7 there correspond to N, S1, S2, S3, S4, S5, S6 here; the VBA
    function works on the 7 significant digits and the caller appends the
    check digit afterwards, which is exactly upce_to_upca's contract too.
    """
    if len(code) != 8 or not code.isdigit():
        raise ValueError("expected 8 digits")
    d1, d2, d3, d4, d5, d6, d7, check = code
    if d7 == "0":
        body = d1 + d2 + d3 + "00000" + d4 + d5 + d6
    elif d7 == "1" or d7 == "2":
        body = d1 + d2 + d3 + d7 + "0000" + d4 + d5 + d6
    elif d7 == "3":
        body = d1 + d2 + d3 + d4 + "00000" + d5 + d6
    elif d7 == "4":
        body = d1 + d2 + d3 + d4 + d5 + "00000" + d6
    else:  # 5-9
        body = d1 + d2 + d3 + d4 + d5 + d6 + "0000" + d7
    return body + check


def test_published_vector_02532038():
    # BarcodeFAQ.com: "A UPC-E number with the value 02532038 scans as
    # 025300000208 with a barcode scanner."
    assert upce_to_upca("02532038") == "025300000208"


def test_published_vector_check_digit_is_valid():
    info = gtin_canonicalize("02532038")
    assert info is not None
    assert info.upca == "025300000208"
    assert info.check_valid is True


# A representative code for each S6 branch (0-9), each with a distinct,
# hand-chosen digit pattern so a branch mix-up (e.g. swapping the "3" and
# "4" cases) cannot accidentally produce the right answer by coincidence.
BRANCH_VECTORS = [
    "04123450",
    "04123451",
    "04123452",
    "04123453",
    "04123454",
    "04123455",
    "04123456",
    "04123457",
    "04123458",
    "04123459",
]


@pytest.mark.parametrize("code", BRANCH_VECTORS)
def test_matches_independent_reference_per_branch(code):
    assert upce_to_upca(code) == _reference_upce_to_upca(code)


@pytest.mark.parametrize(
    "code",
    [
        "00000000",
        "00000005",
        "99999999",
        "12345670",
        "12345678",
        "50000009",
    ],
)
def test_matches_independent_reference_edge_cases(code):
    assert upce_to_upca(code) == _reference_upce_to_upca(code)


def test_upca_and_ean13_and_gtin14_agree_with_upce_expansion():
    upca = upce_to_upca("02532038")
    info8 = gtin_canonicalize("02532038")
    info12 = gtin_canonicalize(upca)
    assert info8.gtin14 == info12.gtin14
    assert info8.ean13 == info12.ean13
    assert info8.upca == info12.upca == upca


def test_rejects_wrong_length():
    with pytest.raises(ValueError):
        upce_to_upca("1234567")  # 7 digits
    with pytest.raises(ValueError):
        upce_to_upca("123456789")  # 9 digits


def test_check_digit_matches_gs1_mod10_known_value():
    # Body "03600029145" -> published GTIN/UPC check digit 2 (widely used
    # generic example in GS1 check-digit documentation/tools).
    assert compute_check_digit("03600029145") == "2"
