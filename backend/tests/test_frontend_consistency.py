"""Static checks on the frontend that would otherwise fail silently in production."""

from __future__ import annotations

import re
from pathlib import Path

from autorack.config import get_settings

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_landing_page_price_matches_billing_constant():
    dollars = get_settings().plan_price_cents // 100
    html = (FRONTEND / "index.html").read_text()
    assert f'<div class="price">${dollars}<span>/month</span>' in html
    assert f"${dollars}/month per warehouse, flat" in html
    assert f"${dollars}/month flat" in html  # meta description and comparison table
    signup = (FRONTEND / "app" / "js" / "auth-pages.js").read_text()
    assert f"${dollars}/month" in signup


def _imports(path: Path) -> set[Path]:
    src = path.read_text()
    found = set()
    for spec in re.findall(r'^\s*import[^"\']*["\'](\.[^"\']+)["\']', src, flags=re.M):
        found.add((path.parent / spec).resolve())
    return found


def test_service_worker_caches_every_module_the_worker_app_loads():
    """If a module is missing from the shell cache, the app can't reload offline."""
    sw = (FRONTEND / "w" / "sw.js").read_text()
    shell = set(re.findall(r'"(/[^"]*)"', sw.split("const SHELL = [")[1].split("];")[0]))
    todo = [FRONTEND / "w" / "js" / "app.js"]
    seen: set[Path] = set()
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        todo.extend(_imports(p))
    for p in seen:
        url = "/" + p.relative_to(FRONTEND).as_posix()
        assert url in shell, f"{url} is not in the service worker's SHELL list"
    html = (FRONTEND / "w" / "index.html").read_text()
    for ref in re.findall(r'(?:href|src)="(/[^"]+\.(?:css|js))"', html):
        assert ref in shell, f"{ref} (from w/index.html) is not cached for offline use"
    for url in shell:
        target = FRONTEND / url.lstrip("/")
        assert target.exists() or url.endswith("/"), f"SHELL lists missing file {url}"


def test_pages_have_no_inline_script_or_style():
    """Our CSP is script-src 'self'; style-src 'self'. Inline code would be blocked."""
    for page in FRONTEND.rglob("*.html"):
        html = page.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), f"inline <script> in {page}"
        assert "<style" not in html, f"inline <style> in {page}"
        assert not re.search(r'\sstyle="', html), f"style attribute in {page}"
        assert not re.search(r"\son[a-z]+=", html), f"inline event handler in {page}"


def test_no_innerhtml_in_frontend_code():
    for js in FRONTEND.rglob("*.js"):
        if "vendor" in js.parts or "tests" in js.parts:
            continue
        assert not re.search(r"\.(innerHTML|outerHTML)\s*[+]?=|insertAdjacentHTML|document\.write", js.read_text()), (
            f"raw HTML insertion in {js}; use shared/dom.js h()"
        )


def test_headers_file_matches_server_csp():
    from autorack.main import CSP

    headers = (FRONTEND / "_headers").read_text()
    expected = CSP.format(api="__API_ORIGIN__")
    assert expected in headers
