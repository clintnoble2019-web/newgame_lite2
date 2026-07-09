"""
NexGame Lite — Customer Admin CLI
Kage Software · 2026

Two buyer populations:
    B2B (Contra) — manual entry, this CLI is the only path
    B2C (Gumroad) — auto-created by gumroad_webhook.py on each sale;
                    this CLI is a manual fallback / inspection tool

Usage:
    python manage_customers.py add             (interactive, either tier)
    python manage_customers.py list             (all customers)
    python manage_customers.py list --b2c        (Gumroad only)
    python manage_customers.py list --b2b        (Contra only)
    python manage_customers.py check                       (dry run)
    python manage_customers.py check --send                (mark contacted)
    python manage_customers.py upgrade <id> pro
"""

import argparse
import uuid
from datetime import date

import customers as cust

TIER_PROMPT = """Tier —
  (s) Season   $450  [B2B, Contra]
  (l) Lifetime $900  [B2B, Contra]
  (b) Basic    $19.99/mo [B2C, Gumroad — normally auto-added by webhook]
  (p) Pro      $39.99/mo [B2C, Gumroad — normally auto-added by webhook]
Choice: """

TIER_MAP = {
    "s": cust.Tier.SEASON, "l": cust.Tier.LIFETIME,
    "b": cust.Tier.MONTHLY_BASIC, "p": cust.Tier.MONTHLY_PRO,
}


def cmd_add(args):
    cust.init_db()
    name = input("Client name: ").strip()
    email = input("Client email: ").strip()
    tier_in = input(TIER_PROMPT).strip().lower()
    tier = TIER_MAP.get(tier_in, cust.Tier.SEASON)
    source = "gumroad" if tier in cust.B2C_TIERS else "contra"

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
    if source == "contra":
        print(f"\n  LOGIN CREDENTIALS — send these to the client:")
        print(f"    Email:       {email}")
        print(f"    License Key: {license_key}")
        print(f"    Login at:    [your dashboard URL]/login")


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

    print(f"\n{'ID':<10} {'Name':<20} {'Tier':<15} {'Status':<10} "
          f"{'Purchased':<12} Email")
    print("-" * 95)
    for c in people:
        print(f"{c.customer_id:<10} {c.name:<20} {c.tier.value:<15} "
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
    new_tier = cust.Tier.MONTHLY_PRO if args.new_tier == "pro" \
        else cust.Tier.MONTHLY_BASIC
    cust.upgrade_tier(args.customer_id, new_tier)
    print(f"✓ {args.customer_id} moved to {new_tier.value}")


def main():
    ap = argparse.ArgumentParser(description="NexGame Lite customer admin")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("add", help="Record a new paid customer (B2B or B2C)")

    p_list = sub.add_parser("list", help="List customers")
    p_list.add_argument("--b2c", action="store_true", help="Gumroad only")
    p_list.add_argument("--b2b", action="store_true", help="Contra only")

    p_check = sub.add_parser("check", help="Preview/send lifecycle messages")
    p_check.add_argument("--send", action="store_true",
                         help="Mark as contacted (default: dry run)")

    p_up = sub.add_parser("upgrade", help="Move a B2C customer Basic<->Pro")
    p_up.add_argument("customer_id")
    p_up.add_argument("new_tier", choices=["basic", "pro"])

    args = ap.parse_args()
    {"add": cmd_add, "list": cmd_list, "check": cmd_check,
     "upgrade": cmd_upgrade}[args.cmd](args)


if __name__ == "__main__":
    main()
