"""Worker-facing translations (English/Spanish).

Scoped deliberately to the worker PWA only -- the owner app and marketing
page are untranslated for now (see README gap analysis). Plain dicts, no
gettext catalogs or extra dependency: consistent with "no build step."

worker_join.html is server-rendered, so it's translated here, in Python.
worker_scan.html runs app.js from the moment it loads, so its strings live
in static/js/worker/i18n.js instead -- one source of truth per rendering
context, not two copies of the same string.
"""

from __future__ import annotations

SUPPORTED_LANGS = ("en", "es")
DEFAULT_LANG = "en"

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "app_title": "Autorack Verify",
        "enter_name_prompt": "Enter your name to start scanning.",
        "name_placeholder": "Your name",
        "start_shift": "Start shift",
        "scan_qr_prompt": "Scan the QR code posted at the dock door to start your shift.",
        "error_qr_not_recognized": "This QR code isn't recognized. Ask your manager to reprint it.",
        "error_shift_closed": "This shift has been closed. Ask your manager for a new QR code.",
        "error_shift_expired": "This shift's QR code has expired. Ask your manager for a new one.",
        "error_account_not_active": "This account is not active. Ask your manager to contact support.",
        "error_shift_not_active": "This shift is no longer active.",
        "error_enter_name": "Enter your name to start your shift.",
        "language_toggle": "Español",
    },
    "es": {
        "app_title": "Autorack Verify",
        "enter_name_prompt": "Escribe tu nombre para empezar a escanear.",
        "name_placeholder": "Tu nombre",
        "start_shift": "Iniciar turno",
        "scan_qr_prompt": "Escanea el código QR en la puerta del muelle para empezar tu turno.",
        "error_qr_not_recognized": "Este código QR no se reconoce. Pide a tu gerente que lo reimprima.",
        "error_shift_closed": "Este turno ha sido cerrado. Pide a tu gerente un nuevo código QR.",
        "error_shift_expired": "El código QR de este turno ha expirado. Pide a tu gerente uno nuevo.",
        "error_account_not_active": "Esta cuenta no está activa. Pide a tu gerente que contacte con soporte.",
        "error_shift_not_active": "Este turno ya no está activo.",
        "error_enter_name": "Escribe tu nombre para empezar tu turno.",
        "language_toggle": "English",
    },
}


def normalize_lang(lang: str | None) -> str:
    if lang and lang.lower() in SUPPORTED_LANGS:
        return lang.lower()
    return DEFAULT_LANG


def detect_lang_from_accept_header(accept_language: str | None) -> str:
    if not accept_language:
        return DEFAULT_LANG
    primary = accept_language.split(",")[0].strip().lower()
    lang_code = primary.split("-")[0]
    return normalize_lang(lang_code)


def t(lang: str, key: str) -> str:
    lang = normalize_lang(lang)
    return STRINGS[lang].get(key, STRINGS[DEFAULT_LANG].get(key, key))


def strings_for(lang: str) -> dict[str, str]:
    """The full string table for a language, for passing to a template as
    one context variable (`{{ s.enter_name_prompt }}`) instead of many
    individual t() calls."""
    lang = normalize_lang(lang)
    return STRINGS[lang]


def other_lang(lang: str) -> str:
    lang = normalize_lang(lang)
    return "es" if lang == "en" else "en"
