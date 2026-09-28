"""Helpers for files people download: CSV cells that can't turn into
spreadsheet formulas, and Content-Disposition headers that survive any
filename (order numbers and company names can be in any script)."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")


def safe_cell(v: Any) -> str:
    """A CSV cell that Excel, Numbers and Sheets show as text. A barcode is
    whatever a label said, and a name is whatever was imported: a leading
    '=' (or + - @) would otherwise run as a formula (=HYPERLINK, =cmd...).
    Plain numbers are left alone so they still add up."""
    s = "" if v is None else str(v)
    if s and s[0] in FORMULA_START and not _is_number(s):
        return "'" + s
    return s


def _is_number(s: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\d+(\.\d+)?", s))


def csv_text(header: list[str], rows: Iterable[Iterable[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([safe_cell(h) for h in header])
    for row in rows:
        w.writerow([safe_cell(c) for c in row])
    return buf.getvalue()


def attachment(filename: str, disposition: str = "attachment") -> str:
    """`attachment; filename="..."` with an ASCII fallback, plus the real
    name (RFC 5987) when it isn't plain ASCII. Header values must be
    Latin-1, so a raw "訂單-1001.pdf" would crash the response."""
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "-", ascii_name).strip(" .-") or "download"
    value = f'{disposition}; filename="{ascii_name[:150]}"'
    if ascii_name != filename:
        value += f"; filename*=UTF-8''{quote(filename[:150], safe='')}"
    return value
