"""
NexGame Lite — Customers
Kage Software · 2026

Two completely separate buyer populations, priced deliberately apart
(FDD v1.3 + locked pricing decision):

    B2B — Contra freelance clients, sold direct, one-time:
        SEASON     -> $450, expires end of 2026 season
        LIFETIME   -> $900, never expires

    B2C — Whop, single product, three billing intervals:
        MONTHLY    -> $39.98/mo, 7-day free trial
        SEMIANNUAL -> $199.99 / 6 months
        ANNUAL     -> $399.99 / year

CHANGED 2026-07-18 per Clint: Whop REPLACES Gumroad entirely as the B2C
billing processor. Chosen because Whop can act as merchant-of-record
(sidesteps the high-risk-processor underwriting problem that blocked
other launches this session) and natively supports free trials on
recurring checkout links with zero custom billing code. The old
Basic/Pro two-tier split is gone, replaced by three billing-INTERVAL
options on one product (not a features split) — that also means the
old UPGRADE_NUDGE lifecycle message (Basic->Pro features) no longer
applies and has been removed; WIN_BACK still does.

THE DECISION TREE THIS MODULE IMPLEMENTS:

    Tier = SEASON or LIFETIME (B2B)
        season still active            -> NONE
        <14 days left in season        -> SEASON_ENDING_SOON
        season ended                   -> RENEWAL_PLEA
        (lifetime) new season released -> LIFETIME_THANKS

    Tier = MONTHLY (B2C)
        active                                  -> NONE
        just cancelled (<= 3 days ago)          -> WIN_BACK
        cancelled, already win-backed once      -> NONE (don't nag)

Whop owns actual billing/renewal for the Monthly tier — this module
only TRACKS status (fed by the Whop webhook, see whop_webhook.py) and
decides what message, if any, a human should see.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum

import config

import os
CUSTOMERS_DB = os.environ.get("CUSTOMERS_DB_PATH", "nexgame_lite_customers.db")

# ── Locked season boundary (B2B) ─────────────────────────────────────
# Update this each year once next season's calendar is confirmed.
SEASON_END = date(2026, 11, 1)

# ── Locked B2C behavior thresholds ───────────────────────────────────
WIN_BACK_WINDOW_DAYS = 3        # send win-back only within N days of cancel


class Tier(Enum):
    # B2B — Contra, one-time
    SEASON = "season"                  # $450
    LIFETIME = "lifetime"              # $900
    # B2C — Whop, one product, three billing intervals
    MONTHLY = "monthly"                # $39.98/mo, 7-day free trial
    SEMIANNUAL = "semiannual"          # $199.99 / 6 months
    ANNUAL = "annual"                  # $399.99 / year


B2B_TIERS = (Tier.SEASON, Tier.LIFETIME)
B2C_TIERS = (Tier.MONTHLY, Tier.SEMIANNUAL, Tier.ANNUAL)

TIER_PRICE = {
    Tier.SEASON: 450.00,
    Tier.LIFETIME: 900.00,
    Tier.MONTHLY: 39.98,
    Tier.SEMIANNUAL: 199.99,
    Tier.ANNUAL: 399.99,
}


class SubStatus(Enum):
    """B2C only — Whop-reported subscription status. TRIALING added
    2026-07-18 for the 7-day free trial window Whop natively supports
    on recurring checkout links — treat the same as ACTIVE for access
    purposes, tracked separately only so lifecycle messaging can
    eventually distinguish a trial user from a paying one if needed."""
    ACTIVE = "active"
    TRIALING = "trialing"
    CANCELLED = "cancelled"
    PAST_DUE = "past_due"
    NA = "n/a"          # B2B customers — not a subscription, always n/a


class MessageAction(Enum):
    NONE = "none"
    # B2B
    RENEWAL_PLEA = "renewal_plea"
    LIFETIME_THANKS = "lifetime_thanks"
    SEASON_ENDING_SOON = "season_ending_soon"
    # B2C
    WIN_BACK = "win_back"


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    tier: Tier
    purchased_at: str              # ISO date — B2C: original subscribe date
    source: str = ""                # 'contra' | 'whop'
    contra_order_id: str = ""
    whop_subscription_id: str = ""
    license_key: str = ""           # login credential — minted for both
                                     # B2B (manual) and B2C (on first
                                     # Whop membership.activated event)
    sub_status: SubStatus = SubStatus.NA
    status_updated_at: str = ""     # last time Whop told us status changed
    win_back_sent: bool = False
    last_contacted_at: str = ""
    notes: str = ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id            TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    email                    TEXT NOT NULL,
    tier                     TEXT NOT NULL,
    purchased_at             TEXT NOT NULL,
    source                   TEXT,
    contra_order_id          TEXT,
    whop_subscription_id     TEXT,
    license_key              TEXT,
    sub_status               TEXT NOT NULL DEFAULT 'n/a',
    status_updated_at        TEXT,
    win_back_sent            INTEGER NOT NULL DEFAULT 0,
    last_contacted_at        TEXT,
    last_action_sent         TEXT,
    notes                    TEXT
);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);
CREATE INDEX IF NOT EXISTS idx_customers_license ON customers(license_key);
"""


@contextmanager
def _db(path: str = None):
    conn = sqlite3.connect(path or CUSTOMERS_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: str = None):
    with _db(path) as db:
        db.executescript(SCHEMA)

        # Migration 2026-07-18: rename gumroad_subscription_id -> 
        # whop_subscription_id on any pre-existing customers table
        # created before the Whop swap. CREATE TABLE IF NOT EXISTS is a
        # no-op when the table already exists, so the schema block
        # above alone doesn't rename anything on live databases — this
        # explicit migration does. Safe to run on fresh DBs too: if
        # the old column isn't there, we skip; if the new column is
        # already there, we skip.
        cols = {r["name"] for r in db.execute(
            "PRAGMA table_info(customers)").fetchall()}
        if "gumroad_subscription_id" in cols and "whop_subscription_id" not in cols:
            db.execute("ALTER TABLE customers "
                       "RENAME COLUMN gumroad_subscription_id "
                       "TO whop_subscription_id")
        elif "whop_subscription_id" not in cols:
            # Fresh DB with neither name (shouldn't happen given
            # SCHEMA above, but defensive) — add the column so
            # _row_to_customer can read it without KeyError.
            db.execute("ALTER TABLE customers ADD COLUMN "
                       "whop_subscription_id TEXT")


def _row_to_customer(r) -> Customer:
    return Customer(
        customer_id=r["customer_id"], name=r["name"], email=r["email"],
        tier=Tier(r["tier"]), purchased_at=r["purchased_at"],
        source=r["source"] or "",
        contra_order_id=r["contra_order_id"] or "",
        whop_subscription_id=r["whop_subscription_id"] or "",
        license_key=r["license_key"] or "",
        sub_status=SubStatus(r["sub_status"] or "n/a"),
        status_updated_at=r["status_updated_at"] or "",
        win_back_sent=bool(r["win_back_sent"]),
        last_contacted_at=r["last_contacted_at"] or "",
        notes=r["notes"] or "")


def add_customer(customer: Customer, path: str = None):
    """Record a paid client.
    B2B (season/lifetime): call manually after a Contra sale. If no
    license_key is given, one is generated so the customer has a login
    credential immediately.
    B2C (monthly/semiannual/annual): call automatically from the Whop
    webhook on 'membership.activated' — Whop doesn't hand us a
    license_key the way Gumroad did, so one is minted here (same
    _generate_license_key() path as B2B).

    sub_status resolution: if the caller already set customer.sub_status
    (webhook passes TRIALING for the 7-day free trial window), honor it.
    Otherwise fall back to a sensible default per tier — ACTIVE for
    B2C (paid, no trial), NA for B2B."""
    if customer.sub_status != SubStatus.NA:
        status = customer.sub_status
    elif customer.tier in B2C_TIERS:
        status = SubStatus.ACTIVE
    else:
        status = SubStatus.NA
    key = customer.license_key or _generate_license_key()
    with _db(path) as db:
        db.execute(
            """INSERT INTO customers
               (customer_id, name, email, tier, purchased_at, source,
                contra_order_id, whop_subscription_id, license_key,
                sub_status, status_updated_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (customer.customer_id, customer.name, customer.email,
             customer.tier.value, customer.purchased_at, customer.source,
             customer.contra_order_id, customer.whop_subscription_id,
             key, status.value,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             customer.notes))
    return key


def _generate_license_key() -> str:
    """Neither B2B (Contra) nor B2C (Whop) customers come with a
    ready-made key from the payment processor, so we mint one in a
    consistent visual format buyers are used to seeing."""
    import secrets
    groups = ["".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
                      for _ in range(4)) for _ in range(4)]
    return "-".join(groups)


def get_customer(customer_id: str, path: str = None) -> Customer | None:
    with _db(path) as db:
        row = db.execute(
            "SELECT * FROM customers WHERE customer_id = ?",
            (customer_id,)).fetchone()
    return _row_to_customer(row) if row else None


def get_by_whop_subscription(sub_id: str, path: str = None) -> Customer | None:
    """Lookup used by the Whop webhook to find who a status event is for."""
    with _db(path) as db:
        row = db.execute(
            "SELECT * FROM customers WHERE whop_subscription_id = ?",
            (sub_id,)).fetchone()
    return _row_to_customer(row) if row else None


def verify_credentials(email: str, license_key: str,
                       path: str = None) -> Customer | None:
    """Login check: email + license_key must both match the same
    customer record. Case-insensitive on email, exact on key."""
    with _db(path) as db:
        row = db.execute(
            """SELECT * FROM customers
               WHERE lower(email) = lower(?) AND license_key = ?""",
            (email.strip(), license_key.strip())).fetchone()
    return _row_to_customer(row) if row else None


def has_active_access(customer: Customer, today: date = None) -> bool:
    """Gate check: does this customer currently have paid access?
    B2B Season: only while before SEASON_END.
    B2B Lifetime: always.
    B2C: ACTIVE or TRIALING (Whop billing owns the actual state —
    trialing customers get full access during the 7-day free trial,
    same as paying ones)."""
    today = today or date.today()
    if customer.tier == Tier.LIFETIME:
        return True
    if customer.tier == Tier.SEASON:
        return today <= SEASON_END
    return customer.sub_status in (SubStatus.ACTIVE, SubStatus.TRIALING)


def all_customers(path: str = None) -> list[Customer]:
    with _db(path) as db:
        rows = db.execute("SELECT * FROM customers").fetchall()
    return [_row_to_customer(r) for r in rows]


def update_sub_status(customer_id: str, status: SubStatus, path: str = None):
    """Called by the Whop webhook when a subscription changes state
    (activated, trial started, cancelled, payment failed). Resets
    win_back_sent when a cancelled customer resubscribes, so a future
    cancel gets a fresh win-back message."""
    with _db(path) as db:
        db.execute(
            """UPDATE customers
               SET sub_status = ?, status_updated_at = ?,
                   win_back_sent = CASE WHEN ? IN ('active', 'trialing') THEN 0
                                        ELSE win_back_sent END
               WHERE customer_id = ?""",
            (status.value,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             status.value, customer_id))


def upgrade_tier(customer_id: str, new_tier: Tier, path: str = None):
    """Basic -> Pro upgrade (or any tier change)."""
    with _db(path) as db:
        db.execute("UPDATE customers SET tier = ? WHERE customer_id = ?",
                   (new_tier.value, customer_id))


def _mark_contacted(customer_id: str, action: MessageAction, path: str = None):
    with _db(path) as db:
        db.execute(
            """UPDATE customers
               SET last_contacted_at = ?, last_action_sent = ?,
                   win_back_sent = CASE WHEN ? = 'win_back' THEN 1
                                        ELSE win_back_sent END
               WHERE customer_id = ?""",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             action.value, action.value, customer_id))


# ── THE DECISION NODE ─────────────────────────────────────────────────
def determine_action(customer: Customer, today: date = None) -> MessageAction:
    """
    THE decision this whole module exists for. Branches on buyer
    population first (B2B vs B2C), then tier-specific logic.
    """
    today = today or date.today()

    # ── B2B branch: Season / Lifetime (Contra) ──────────────────────
    if customer.tier in B2B_TIERS:
        days_left = (SEASON_END - today).days
        if customer.tier == Tier.LIFETIME:
            if today > SEASON_END:
                return MessageAction.LIFETIME_THANKS
            return MessageAction.NONE
        # SEASON
        if today > SEASON_END:
            return MessageAction.RENEWAL_PLEA
        if 0 <= days_left <= 14:
            return MessageAction.SEASON_ENDING_SOON
        return MessageAction.NONE

    # ── B2C branch: Monthly (Whop) ───────────────────────────────────
    if customer.sub_status == SubStatus.CANCELLED:
        if customer.win_back_sent:
            return MessageAction.NONE
        cancelled_at = _parse_date(customer.status_updated_at, today)
        if (today - cancelled_at).days <= WIN_BACK_WINDOW_DAYS:
            return MessageAction.WIN_BACK
        return MessageAction.NONE   # missed the window, don't nag later

    return MessageAction.NONE   # active, trialing, or past_due — Whop
                                # handles retry/dunning on its own


def _parse_date(iso_str: str, fallback: date) -> date:
    if not iso_str:
        return fallback
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).date()
    except ValueError:
        return fallback


# ── Message templates — B2B ─────────────────────────────────────────
RENEWAL_PLEA_TEMPLATE = """\
Subject: Your NexGame Lite season access has ended — renew for 2027

Hi {name},

Your NexGame Lite season access wrapped with the 2026 MLB/NBA season.
Thanks for being one of our earliest clients — your usage (and feedback)
directly shaped what's coming in full NexGame.

Ready for another season? Renew for $450 (season) or upgrade to
lifetime for $900 — no more renewal emails, ever.

Reply here or re-order through Contra whenever you're ready.

— Clint, Kage Software
"""

LIFETIME_THANKS_TEMPLATE = """\
Subject: New season is live — your lifetime access, updated

Hi {name},

The 2027 season data is live inside NexGame Lite. As a lifetime member,
there's nothing to renew and nothing to pay — it's already active on
your dashboard.

New this season: {season_notes}

Thanks again for being a founding NexGame Lite client. It means a lot.

— Clint, Kage Software
"""

SEASON_ENDING_SOON_TEMPLATE = """\
Subject: Your NexGame Lite season access ends in {days_left} days

Hi {name},

Just a heads-up — your season access wraps up on {season_end}.
If you've found value in the predictions, you can lock in lifetime
access now for $900 (no more renewals) or plan to renew for $450
when the next season begins.

— Clint, Kage Software
"""

# ── Message templates — B2C ─────────────────────────────────────────
WIN_BACK_TEMPLATE = """\
Subject: Sorry to see you go — quick question

Hi {name},

Noticed your NexGame Lite subscription just ended. No hard feelings —
just curious if it was a price thing, a features thing, or just bad
timing with the season. A quick reply helps a lot.

If you want back in, your account and settings are still here whenever
you're ready.

— Clint, Kage Software
"""


def build_message(customer: Customer, action: MessageAction,
                  today: date = None, season_notes: str = "") -> str:
    """Render the actual email text for a given customer + action."""
    today = today or date.today()
    if action == MessageAction.RENEWAL_PLEA:
        return RENEWAL_PLEA_TEMPLATE.format(name=customer.name)
    if action == MessageAction.LIFETIME_THANKS:
        return LIFETIME_THANKS_TEMPLATE.format(
            name=customer.name,
            season_notes=season_notes or "refreshed rosters and rolling stats.")
    if action == MessageAction.SEASON_ENDING_SOON:
        days_left = (SEASON_END - today).days
        return SEASON_ENDING_SOON_TEMPLATE.format(
            name=customer.name, days_left=days_left,
            season_end=SEASON_END.isoformat())
    if action == MessageAction.WIN_BACK:
        return WIN_BACK_TEMPLATE.format(name=customer.name)
    return ""


def run_lifecycle_check(path: str = None, today: date = None,
                        dry_run: bool = True) -> list[dict]:
    """
    Walk every customer (B2B and B2C alike), determine their action,
    build their message. dry_run=True (default): preview only, sends
    and marks nothing. Flip dry_run=False once wired to real sending
    (SES for B2B, or Whop's own email tools for B2C).
    """
    today = today or date.today()
    out = []
    for c in all_customers(path):
        action = determine_action(c, today)
        if action == MessageAction.NONE:
            continue
        message = build_message(c, action, today)
        out.append({
            "customer_id": c.customer_id, "name": c.name,
            "email": c.email, "tier": c.tier.value,
            "action": action.value, "message": message,
        })
        if not dry_run:
            _mark_contacted(c.customer_id, action, path)
    return out
