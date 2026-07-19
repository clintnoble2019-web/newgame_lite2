"""
NexGame Lite — Customer Admin CLI
Kage Software · 2026

Two buyer populations:
    B2B (Contra) — manual entry, this CLI is the only path
    B2C (Whop)   — auto-created by whop_webhook.py on each
                   membership.activated event; this CLI is a manual
                   fallback / inspection tool for adding someone who
                   already paid but isn't showing up automatically yet
                   (e.g. webhook delay, or a manual/off-platform sale).

REBUILT 2026-07-19 — the previous version of this file was written
before the Gumroad -> Whop billing swap and still referenced
Tier.MONTHLY_BASIC/MONTHLY_PRO, which don't exist anymore (real tiers
are now MONTHLY/SEMIANNUAL/ANNUAL — see customers.py's Tier enum).
Running `add` crashed on startup as a result. Every tier/source
reference below is rechecked against customers.py's actual current
schema, not assumed.

Usage:
    python manage_customers.py add               (interactive, any tier)
    python manage_customers.py list               (all customers)
    python manage_customers.py list --b2c          (Whop only)
    python manage_customers.py list --b2b          (Contra only)
    python manage_customers.py check                         (dry run)
    python manage_customers.py check --send                  (mark contacted)
    python manage_customers.py upgrade <id> monthly|semiannual|annual
    python manage_customers.py reset-key <id>
"""

import argparse
import uuid
from datetime import date

import customers as cust

TIER_PROMPT = """Tier —
  (s) Season      $450     [B2B, Contra]
  (l) Lifetime    $900     [B2B, Contra]
  (m) Monthly     $39.98/mo   [B2C, Whop — normally auto-added by webhook]
  (6) 6-Month     $199.99     [B2C, Whop — normally auto-added by webhook]
  (a) Annual      $399.99     [B2C, Whop — normally auto-added by webhook]
Choice: """

TIER_MAP = {
    "s": cust.Tier.SEASON, "l": cust.Tier.LIFETIME,
    "m": cust.Tier.MONTHLY, "6": cust.Tier.SEMIANNUAL,
    "a": cust.Tier.ANNUAL,
}

UPGRADE_TIER_MAP = {
    "monthly": cust.Tier.MONTHLY,
    "semiannual": cust.Tier.SEMIANNUAL,
    "annual": cust.Tier.ANNUAL,
}


def cmd_add(args):
    cust.init_db()
    name = input("Client name: ").strip()
    email = input("Client email: ").strip()
    tier_in = input(TIER_PROMPT).strip().lower()
    tier = TIER_MAP.get(tier_in, cust.Tier.SEASON)
    source = "whop" if tier in cust.B2C_TIERS else "contra"

    order_id = ""
    if source == "contra":
        order_id = input("Contra order ID (optional): ").strip()

    customer = cust.Customer(
        customer_id=str(uuid.uuid4())[:8],
        name=name, email=email, tier=tier,
        purchased_at=date.today().isoformat(),
        source=source, contra_order_id=order_id,
    )
    license_key = cust.add_customer(customer)
    price = cust.TIER_PRICE[tier]
    print(f"\n✓ Added {name} ({tier.value}, ${price}) "
          f"— id {customer.customer_id}")
    print(f"\n  LOGIN CREDENTIALS — send these to the client:")
    print(f"    Email:       {email}")
    print(f"    License Key: {license_key}")
    print(f"    Login at:    https://nexgamelite.com/login")
    if source == "whop":
        print(f"\n  Note: this customer will ALSO be able to use "
              f"'Sign in with Whop' if their email matches a real "
              f"Whop membership — the license key above is a fallback, "
              f"not the only way in.")


def cmd_list(args):
    cust.init_db()
    people = cust.all_customers()
    if args.b2c:
        people = [c for c in people if c.tier in cust.B2C_TIERS]
    if args.b2b:
        people = [c for c in people if c.tier in cust.B2B_TIERS]
    if not people:
        print("No customers found.")
        return

    print(f"\n{'ID':<10} {'Name':<20} {'Tier':<12} {'Status':<10} "
          f"{'Purchased':<12} Email")
    print("-" * 95)
    for c in people:
        print(f"{c.customer_id:<10} {c.name:<20} {c.tier.value:<12} "
              f"{c.sub_status.value:<10} {c.purchased_at:<12} {c.email}")

    b2b_ct = sum(1 for c in people if c.tier in cust.B2B_TIERS)
    b2c_ct = sum(1 for c in people if c.tier in cust.B2C_TIERS)
    print(f"\nTotal: {len(people)}  ({b2b_ct} B2B / {b2c_ct} B2C)")


def cmd_check(args):
    cust.init_db()
    results = cust.run_lifecycle_check(dry_run=not args.send)
    if not results:
        print("No messages needed right now — everyone's current.")
        return
    for r in results:
        print(f"\n{'='*64}")
        print(f"  {r['name']} <{r['email']}>  [{r['tier']}] -> {r['action']}")
        print(f"{'='*64}")
        print(r["message"])
    mode = "SENT (marked contacted)" if args.send else "DRY RUN — nothing sent"
    print(f"\n{len(results)} message(s) generated. Mode: {mode}")


def cmd_upgrade(args):
    cust.init_db()
    new_tier = UPGRADE_TIER_MAP[args.new_tier]
    cust.upgrade_tier(args.customer_id, new_tier)
    print(f"✓ {args.customer_id} moved to {new_tier.value}")


def cmd_reset_key(args):
    """Fixes 'customer says login fails' without deleting/recreating
    them — list doesn't print existing keys back out on purpose, so
    this is the actual troubleshooting path."""
    cust.init_db()
    new_key = cust.reset_license_key(args.customer_id)
    if new_key is None:
        print(f"No customer found with id {args.customer_id!r} — "
             f"check 'python manage_customers.py list' for the real id.")
        return
    print(f"\n✓ New license key generated for {args.customer_id} "
         f"— the old one no longer works.")
    print(f"    License Key: {new_key}")
    print(f"    Login at:    https://nexgamelite.com/login")


def main():
    ap = argparse.ArgumentParser(description="NexGame Lite customer admin")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("add", help="Record a new paid customer (B2B or B2C)")

    p_list = sub.add_parser("list", help="List customers")
    p_list.add_argument("--b2c", action="store_true", help="Whop only")
    p_list.add_argument("--b2b", action="store_true", help="Contra only")

    p_check = sub.add_parser("check", help="Preview/send lifecycle messages")
    p_check.add_argument("--send", action="store_true",
                         help="Mark as contacted (default: dry run)")

    p_up = sub.add_parser("upgrade", help="Change a B2C customer's tier")
    p_up.add_argument("customer_id")
    p_up.add_argument("new_tier", choices=["monthly", "semiannual", "annual"])

    p_reset = sub.add_parser("reset-key",
        help="Mint a fresh license key (fixes 'login doesn't work' reports)")
    p_reset.add_argument("customer_id")

    args = ap.parse_args()
    {"add": cmd_add, "list": cmd_list, "check": cmd_check,
     "upgrade": cmd_upgrade, "reset-key": cmd_reset_key}[args.cmd](args)


if __name__ == "__main__":
    main()
