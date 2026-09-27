"""'Contact support' from the dashboard: the message is emailed to
SUPPORT_EMAIL (or OPERATOR_EMAILS) with the account details attached, and
Reply-To set to the sender, so answering is just replying."""

from __future__ import annotations

import html
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import OwnerContext, current_member
from ..errors import ApiError
from ..services import email, ratelimit

router = APIRouter(tags=["support"])

TOPICS = {
    "question": "Question",
    "problem": "Something isn't working",
    "billing": "Billing",
    "idea": "Feature idea",
}


class SupportIn(BaseModel):
    topic: Literal["question", "problem", "billing", "idea"] = "question"
    message: str = Field(min_length=5, max_length=5000)
    page: str | None = Field(default=None, max_length=200)


def support_recipients() -> list[str]:
    s = get_settings()
    if s.support_email.strip():
        return [s.support_email.strip()]
    return sorted(s.operator_email_set)


@router.post("/support")
def contact_support(
    body: SupportIn,
    request: Request,
    ctx: OwnerContext = Depends(current_member),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    to = support_recipients()
    if not to:
        raise ApiError(503, "support_unavailable", "Support email isn't set up yet.")
    ratelimit.check_db(
        db, "support", str(ctx.user.id), 5, timedelta(hours=1), "That's a lot of messages. Please wait a bit."
    )
    db.commit()
    wh = ctx.warehouse
    topic = TOPICS[body.topic]
    details = [
        ("From", ctx.user.email),
        ("Role", ctx.role.value),
        ("Warehouse", f"{wh.name} ({wh.id})"),
        ("Plan", f"{wh.subscription_status.value}, {wh.billing_interval or 'no subscription'}"),
        ("Page", body.page or "-"),
        ("Browser", (request.headers.get("user-agent") or "-")[:200]),
    ]
    text = body.message.strip() + "\n\n--\n" + "\n".join(f"{k}: {v}" for k, v in details)
    body_html = f"<p style='white-space:pre-wrap'>{html.escape(body.message.strip())}</p><hr>" + "".join(
        f"<div><b>{html.escape(k)}:</b> {html.escape(v)}</div>" for k, v in details
    )
    for addr in to:
        email.send(
            email.Email(
                to=addr,
                subject=f"[Autorack support] {topic} — {wh.name}",
                text=text,
                html=body_html,
                reply_to=ctx.user.email,
            )
        )
    return {"sent": True}
