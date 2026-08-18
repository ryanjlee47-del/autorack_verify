"""Single source of truth for pricing.

The billing engine, the owner savings dashboard, and the public marketing
page all import these constants directly -- none of them may hardcode a
dollar figure or a free-catch count anywhere else. Marketing copy that
contradicts billing reality is a defect: see
tests/test_marketing_pricing_consistency.py, which renders the actual
landing page and fails if a displayed number doesn't trace back to here.

These are *defaults*. Per-account overrides (accounts.price_per_catch_cents,
accounts.free_allowance) are stored in the database and can be changed by
the operator per account/plan -- these constants are what a brand-new
account is seeded with, and what the marketing page advertises to
prospects who don't have an account yet.
"""

DEFAULT_PLAN_NAME = "standard"

# A prevented mis-ship costs the warehouse real money: the item itself,
# outbound freight, return freight/refund/replacement, and the labor to
# untangle it. $45 is a conservative blended estimate used only for the
# "money saved" framing on the dashboard and marketing page -- it is never
# used to compute what the customer is actually billed.
SAVINGS_PER_CATCH_CENTS = 4500

# What we charge per prevented mis-ship, after the free allowance.
DEFAULT_PRICE_PER_CATCH_CENTS = 900

# Free catches before billing kicks in for a new account.
DEFAULT_FREE_ALLOWANCE = 25


def format_cents_as_dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"
