"""Names people type that end up in other people's inboxes.

Warehouse and client names go into invitation emails (subject and heading)
sent from Autorack's own domain. A name like "Account locked - verify at
paypal-help.example" would make those emails a phishing tool, so names can't
carry web addresses, email addresses or control characters.
"""

from __future__ import annotations

import re

from .errors import bad_request

LINKISH = re.compile(
    r"(https?:|www\.|@|://"
    r"|\b[a-z0-9-]{2,}\.(com|net|org|io|co|ru|xyz|info|biz|app|link|me|us|uk|de|cn|top|site|online|shop|store"
    r"|click|live|help|support|page|dev|ly|gl|tk|cc|to)\b)",
    re.IGNORECASE,
)


def plain_name(value: str, what: str = "name") -> str:
    name = " ".join((value or "").split())
    if not name:
        raise bad_request("name_required", f"Enter a {what}.")
    if LINKISH.search(name) or any(ord(ch) < 32 for ch in value or ""):
        raise bad_request("name_invalid", f"A {what} can't contain a web or email address.")
    return name
