"""
NexGame Lite — Customers
Kage Software · 2026

Two completely separate buyer populations, priced deliberately apart
(FDD v1.3 + locked pricing decision):

    B2B — Contra freelance clients, sold direct, one-time:
        SEASON     -> $450, expires end of 2026 season
        LIFETIME   -> $900, never expires

    B2C — Gumroad Discover strangers, monthly, matches full NexGame's
    locked subscription pricing so today's Lite subscriber already
    knows the price when they graduate to full NexGame in June 2027:
        MONTHLY_BASIC -> $19.99/mo, recurring, billed by Gumroad
        MONTHLY_PRO   -> $39.99/mo, recurring, billed by Gumroad

THE DECISION TREE THIS MODULE IMPLEMENTS:

    Tier = SEASON or LIFETIME (B2B)
        season still active            -> NONE
        <14 days left in season        -> SEASON_ENDING_SOON
        season ended                   -> RENEWAL_PLEA
        (lifetime) new season released -> LIFETIME_THANKS

    Tier = MONTHLY_BASIC or MONTHLY_PRO (B2C)
        active, subscribed < 60 days           -> NONE
        active, Basic, subscribed >= 60 days   -> UPGRADE_NUDGE (Basic->Pro only)
        just cancelled (<= 3 days ago)          -> WIN_BACK
        cancelled, already win-backed once      -> NONE (don't nag)

Gumroad owns actual billing/renewal for Monthly tiers — this module
only TRACKS status (fed by the Gumroad webhook, see gumroad_webhook.py)
and decides what message, if any, a human should see.
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
UPGRADE_NUDGE_AFTER_DAYS = 60   # Basic subscriber tenure before nudging Pro
WIN_BACK_WINDOW_DAYS = 3        # send win-back only within N days of cancel


class Tier(Enum):
    # B2B — Contra, one-time
    SEASON = "season"                  # $450
    LIFETIME = "lifetime"              # $900
    # B2C — Gumroad, monthly recurring
    MONTHLY_BASIC = "monthly_basic"    # $19.99/mo
    MONTHLY_PRO = "monthly_pro"        # $39.99/mo


B2B_TIERS = (Tier.SEASON, Tier.LIFETIME)
B2C_TIERS = (Tier.MONTHLY_BASIC, Tier.MONTHLY_PRO)

TIER_PRICE = {
    Tier.SEASON: 450.00,
    Tier.LIFETIME: 900.00,
    Tier.MONTHLY_BASIC: 19.99,
    Tier.MONTHLY_PRO: 39.99,
}


class SubStatus(Enum):
    """B2C only — Gumroad-reported subscription status."""
    ACTIVE = "active"
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
    UPGRADE_NUDGE = "upgrade_nudge"
    WIN_BACK = "win_back"


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    tier: Tier
    purchased_at: str              # ISO date — B2C: original subscribe date
    source: str = ""                # 'contra' | 'gumroad' | 'whop'
    contra_order_id: str = ""
    gumroad_subscription_id: str = ""
    whop_membership_id: str = ""    # set when source == 'whop' — Whop's
                                     # membership.id, same role as
                                     # gumroad_subscription_id above
    license_key: str = ""           # login credential — Gumroad-issued (B2C)
                                     # or manually assigned (B2B)
    sub_status: SubStatus = SubStatus.NA
    status_updated_at: str = ""     # last time Gumroad told us status changed
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
    gumroad_subscription_id  TEXT,
    whop_membership_id       TEXT,
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
CREATE INDEX IF NOT EXISTS idx_customers_whop ON customers(whop_membership_id);
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
        # Migration: whop_membership_id added 2026-07-18 — CREATE TABLE
        # IF NOT EXISTS above only builds this column on a brand-new
        # database. Existing deployments need it added explicitly.
        try:
            db.execute("ALTER TABLE customers ADD COLUMN "
                      "whop_membership_id TEXT")
        except sqlite3.OperationalError:
            pass   # column already exists


def _row_to_customer(r) -> Customer:
    return Customer(
        customer_id=r["customer_id"], name=r["name"], email=r["email"],
        tier=Tier(r["tier"]), purchased_at=r["purchased_at"],
        source=r["source"] or "",
        contra_order_id=r["contra_order_id"] or "",
        gumroad_subscription_id=r["gumroad_subscription_id"] or "",
        whop_membership_id=r["whop_membership_id"] or "",
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
    B2C (monthly): call automatically from the Gumroad webhook on the
    'sale' event (Gumroad supplies the license_key itself), or from
    the Whop webhook on 'membership.went_valid' (a license_key is
    generated the same way B2B customers get one, since Whop doesn't
    issue one itself the way Gumroad does)."""
    status = (SubStatus.ACTIVE if customer.tier in B2C_TIERS
              else SubStatus.NA)
    key = customer.license_key or _generate_license_key()
    with _db(path) as db:
        db.execute(
            """INSERT INTO customers
               (customer_id, name, email, tier, purchased_at, source,
                contra_order_id, gumroad_subscription_id,
                whop_membership_id, license_key,
                sub_status, status_updated_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (customer.customer_id, customer.name, customer.email,
             customer.tier.value, customer.purchased_at, customer.source,
             customer.contra_order_id, customer.gumroad_subscription_id,
             customer.whop_membership_id,
             key, status.value,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             customer.notes))
    return key


def _generate_license_key() -> str:
    """B2B customers don't come with a Gumroad-issued key, so we mint
    one in the same visual format buyers are used to seeing."""
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


def get_by_gumroad_subscription(sub_id: str, path: str = None) -> Customer | None:
    """Lookup used by the Gumroad webhook to find who a status event is for."""
    with _db(path) as db:
        row = db.execute(
            "SELECT * FROM customers WHERE gumroad_subscription_id = ?",
            (sub_id,)).fetchone()
    return _row_to_customer(row) if row else None


def get_by_whop_membership(membership_id: str, path: str = None) -> Customer | None:
    """Lookup used by the Whop webhook to find who a status event is for.
    Mirrors get_by_gumroad_subscription above — same role, different
    source. Works identically whether the membership originated from
    Whop's own Discover marketplace or an embedded checkout on
    nexgamelite.com; Whop fires the same events either way, so there's
    no special-casing needed here for where the sale happened."""
    with _db(path) as db:
        row = db.execute(
            "SELECT * FROM customers WHERE whop_membership_id = ?",
            (membership_id,)).fetchone()
    return _row_to_customer(row) if row else None


def get_by_email(email: str, path: str = None) -> Customer | None:
    """Case-insensitive email lookup. Used by whop_oauth.py: the real
    Whop 'check user access' endpoint (confirmed 2026-07-18 against
    docs.whop.com) returns has_access/access_level, NOT a membership
    ID — so the OAuth login path can't match against
    whop_membership_id the way the webhook path does. Email is the
    field both paths reliably share (Whop's webhook payload and its
    OAuth userinfo endpoint both carry it), so it's the match key
    here instead."""
    with _db(path) as db:
        row = db.execute(
            "SELECT * FROM customers WHERE lower(email) = lower(?)",
            (email.strip(),)).fetchone()
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
    B2C: only while sub_status is ACTIVE (Gumroad billing owns this)."""
    today = today or date.today()
    if customer.tier == Tier.LIFETIME:
        return True
    if customer.tier == Tier.SEASON:
        return today <= SEASON_END
    return customer.sub_status == SubStatus.ACTIVE


def all_customers(path: str = None) -> list[Customer]:
    with _db(path) as db:
        rows = db.execute("SELECT * FROM customers").fetchall()
    return [_row_to_customer(r) for r in rows]


def update_sub_status(customer_id: str, status: SubStatus, path: str = None):
    """Called by the Gumroad webhook when a subscription changes state
    (renewed, cancelled, payment failed). Resets win_back_sent when a
    cancelled customer resubscribes, so a future cancel gets a fresh
    win-back message."""
    with _db(path) as db:
        db.execute(
            """UPDATE customers
               SET sub_status = ?, status_updated_at = ?,
                   win_back_sent = CASE WHEN ? = 'active' THEN 0
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

    # ── B2C branch: Monthly Basic / Pro (Gumroad) ───────────────────
    if customer.sub_status == SubStatus.CANCELLED:
        if customer.win_back_sent:
            return MessageAction.NONE
        cancelled_at = _parse_date(customer.status_updated_at, today)
        if (today - cancelled_at).days <= WIN_BACK_WINDOW_DAYS:
            return MessageAction.WIN_BACK
        return MessageAction.NONE   # missed the window, don't nag later

    if customer.sub_status == SubStatus.ACTIVE:
        if customer.tier == Tier.MONTHLY_BASIC:
            subscribed_at = _parse_date(customer.purchased_at, today)
            if (today - subscribed_at).days >= UPGRADE_NUDGE_AFTER_DAYS:
                return MessageAction.UPGRADE_NUDGE
        return MessageAction.NONE

    return MessageAction.NONE   # past_due — Gumroad handles retry/dunning


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
UPGRADE_NUDGE_TEMPLATE = """\
Subject: You've been with NexGame Lite for 2 months — see what Pro unlocks

Hi {name},

You've been on Basic ($19.99/mo) for about two months now. Pro
($39.99/mo) adds breakout alerts and full historical prediction
archive access — the stuff serious users end up wanting most.

Upgrade any time from your Gumroad subscription — no new purchase,
just a plan change.

— Clint, Kage Software
"""

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
    if action == MessageAction.UPGRADE_NUDGE:
        return UPGRADE_NUDGE_TEMPLATE.format(name=customer.name)
    if action == MessageAction.WIN_BACK:
        return WIN_BACK_TEMPLATE.format(name=customer.name)
    return ""


def run_lifecycle_check(path: str = None, today: date = None,
                        dry_run: bool = True) -> list[dict]:
    """
    Walk every customer (B2B and B2C alike), determine their action,
    build their message. dry_run=True (default): preview only, sends
    and marks nothing. Flip dry_run=False once wired to real sending
    (SES for B2B, or Gumroad's own email tools for B2C).
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
