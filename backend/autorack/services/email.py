"""Outbound email: magic links and invitations.

Backends:
  console  -- log the message (development; the link appears in the API log)
  memory   -- keep it in `outbox` (tests)
  smtp     -- any SMTP relay (Postmark, SES, Mailgun, Gmail Workspace...)
  resend   -- Resend's HTTP API
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from ..config import get_settings

log = logging.getLogger("autorack.email")


@dataclass
class Email:
    to: str
    subject: str
    text: str
    html: str


outbox: list[Email] = []


class EmailError(RuntimeError):
    pass


def send(msg: Email) -> None:
    s = get_settings()
    if s.email_backend == "memory":
        outbox.append(msg)
    elif s.email_backend == "console":
        log.warning("EMAIL to=%s subject=%r\n%s", msg.to, msg.subject, msg.text)
    elif s.email_backend == "smtp":
        _send_smtp(msg)
    elif s.email_backend == "resend":
        _send_resend(msg)
    else:  # pragma: no cover - guarded by the Literal type in config
        raise EmailError(f"Unknown email backend {s.email_backend}")


def _send_smtp(msg: Email) -> None:
    s = get_settings()
    em = EmailMessage()
    em["From"] = s.email_from
    em["To"] = msg.to
    em["Subject"] = msg.subject
    em.set_content(msg.text)
    em.add_alternative(msg.html, subtype="html")
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
            if s.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(em)
    except (OSError, smtplib.SMTPException) as e:
        raise EmailError(str(e)) from e


def _send_resend(msg: Email) -> None:
    s = get_settings()
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {s.resend_api_key}"},
            json={"from": s.email_from, "to": [msg.to], "subject": msg.subject, "text": msg.text, "html": msg.html},
            timeout=15,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise EmailError(str(e)) from e


def _layout(heading: str, body_html: str, button_label: str, url: str, footer: str) -> str:
    return f"""<!doctype html><html><body style="margin:0;background:#f7f7f5;font-family:Inter,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#162238">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" style="max-width:480px;background:#fff;border-radius:6px;border-top:3px solid #162238;padding:32px" cellpadding="0" cellspacing="0">
<tr><td style="font-weight:700;font-size:16px;letter-spacing:.04em;color:#162238">AUTORACK<div style="font-size:9px;font-weight:600;letter-spacing:.22em;color:#3e7bfa;padding-top:3px">VERIFIED LOGISTICS</div></td></tr>
<tr><td style="padding-top:20px;font-size:21px;font-weight:700">{html.escape(heading)}</td></tr>
<tr><td style="padding-top:12px;font-size:15px;line-height:1.5">{body_html}</td></tr>
<tr><td style="padding-top:24px"><a href="{html.escape(url, quote=True)}" style="display:inline-block;background:#3e7bfa;color:#fff;text-decoration:none;font-weight:600;padding:12px 22px;border-radius:4px">{html.escape(button_label)}</a></td></tr>
<tr><td style="padding-top:24px;font-size:13px;color:#6b7486;line-height:1.5">{html.escape(footer)}</td></tr>
</table></td></tr></table></body></html>"""


def magic_link_email(to: str, url: str, warehouse_name: str) -> Email:
    minutes = get_settings().magic_link_ttl_minutes
    footer = (
        f"This link works once and expires in {minutes} minutes. "
        "If you didn't ask to sign in, you can ignore this email."
    )
    return Email(
        to=to,
        subject="Your Autorack sign-in link",
        text=f"Sign in to Autorack ({warehouse_name}):\n\n{url}\n\n{footer}\n",
        html=_layout(
            "Sign in to Autorack",
            f"Use the button below to sign in to <b>{html.escape(warehouse_name)}</b>.",
            "Sign in",
            url,
            footer,
        ),
    )


def invite_email(to: str, url: str, warehouse_name: str, inviter: str) -> Email:
    minutes = get_settings().magic_link_ttl_minutes
    footer = (
        f"This link expires in {minutes} minutes. You can always request a new one "
        "from the sign-in page with this email address."
    )
    return Email(
        to=to,
        subject=f"You've been added to {warehouse_name} on Autorack",
        text=f"{inviter} added you to {warehouse_name} on Autorack.\n\nSign in: {url}\n\n{footer}\n",
        html=_layout(
            f"Join {warehouse_name}",
            f"{html.escape(inviter)} added you to <b>{html.escape(warehouse_name)}</b> on Autorack, "
            "the pick verification dashboard.",
            "Sign in",
            url,
            footer,
        ),
    )


def _rows_html(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<tr><td style="padding:7px 0;border-bottom:1px solid #e6e8ec;color:#4a5468">{html.escape(k)}</td>'
        f'<td align="right" style="padding:7px 0;border-bottom:1px solid #e6e8ec;font-weight:600">{html.escape(v)}</td></tr>'
        for k, v in rows
    )
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px;font-size:14px">{cells}</table>'


def notice_email(
    to: str,
    subject: str,
    heading: str,
    lines: list[str],
    button_label: str,
    url: str,
    footer: str,
    rows: list[tuple[str, str]] | None = None,
    extra_text: str = "",
) -> Email:
    """A plain notification: a few sentences, an optional table of numbers,
    one button. Used by the daily summary, alerts and account emails."""
    body = "".join(f'<p style="margin:0 0 10px">{html.escape(line)}</p>' for line in lines)
    if rows:
        body += _rows_html(rows)
    if extra_text:
        body += f'<p style="margin:14px 0 0;white-space:pre-line">{html.escape(extra_text)}</p>'
    text = "\n\n".join(lines)
    if rows:
        text += "\n\n" + "\n".join(f"{k}: {v}" for k, v in rows)
    if extra_text:
        text += "\n\n" + extra_text
    text += f"\n\n{button_label}: {url}\n\n{footer}\n"
    return Email(to=to, subject=subject, text=text, html=_layout(heading, body, button_label, url, footer))
