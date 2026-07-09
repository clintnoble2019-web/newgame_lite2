"""
NexGame Lite — Gumroad Webhook Receiver
Kage Software · 2026

Receives Gumroad's "Ping" webhook events and feeds them into customers.py.
This is what keeps sub_status (active/cancelled/past_due) live for the
B2C Monthly tiers without you touching anything by hand.

SETUP AT LAUNCH:
    1. Deploy this alongside api/main.py (same FastAPI app, or standalone)
    2. In Gumroad: Settings > Advanced > Ping endpoint
       -> paste this server's /webhooks/gumroad URL
    3. Gumroad fires a POST on: sale, refund, dispute, cancellation,
       subscription_updated (renewal/plan change)
    4. Verify the signature (GUMROAD_WEBHOOK_SECRET in config.py) before
       trusting the payload — Gumroad signs pings with HMAC-SHA256.

Gumroad's ping payload is form-encoded (not JSON) and includes fields
like: sale_id, product_id, product_permalink, email, price, subscription_id,
recurrence, cancelled (bool string), variants (which tier they bought).
Field names verified against Gumroad's public webhook documentation —
confirm exact field names against a real test ping before going live
(Gumroad's "Send test ping" button in Settings > Advanced).
"""

import hashlib
import hmac
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Request, HTTPException

import config
import customers as cust

router = APIRouter()

# Maps Gumroad's product variant name -> our Tier enum.
# Set these to match EXACTLY what you name the variants in the Gumroad
# product editor (Variants section) when you build the listing.
VARIANT_TIER_MAP = {
    "Basic — $19.99/mo": cust.Tier.MONTHLY_BASIC,
    "Pro — $39.99/mo": cust.Tier.MONTHLY_PRO,
}


def _verify_signature(raw_body: bytes, signature: str) -> bool:
    if not config.GUMROAD_WEBHOOK_SECRET:
        return True  # not configured yet — dev mode, skip verification
    expected = hmac.new(
        config.GUMROAD_WEBHOOK_SECRET.encode(), raw_body,
        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _tier_from_variant(variant_name: str) -> cust.Tier:
    return VARIANT_TIER_MAP.get(variant_name, cust.Tier.MONTHLY_BASIC)


@router.post("/webhooks/gumroad")
async def gumroad_ping(request: Request):
    """Single endpoint handling all Gumroad ping event types.
    Gumroad doesn't send a distinct 'event type' field on the base Ping
    endpoint the way Zapier triggers do — event context is inferred from
    which fields are present (e.g. 'refunded'=='true', 'cancelled'=='true').
    """
    raw = await request.body()
    signature = request.headers.get("X-Gumroad-Signature", "")
    if not _verify_signature(raw, signature):
        raise HTTPException(403, "Invalid webhook signature")

    form = await request.form()
    data = dict(form)

    cust.init_db()
    subscription_id = data.get("subscription_id", "")
    email = data.get("email", "")
    is_cancelled = data.get("cancelled", "false").lower() == "true"
    is_refunded = data.get("refunded", "false").lower() == "true"
    is_ended = data.get("ended", "false").lower() == "true"  # sub fully lapsed

    existing = (cust.get_by_gumroad_subscription(subscription_id)
               if subscription_id else None)

    # ── New sale, no existing subscription on file -> new customer ──
    if not existing and not is_cancelled and not is_refunded:
        variant = data.get("variants", "") or data.get("variants[Tier]", "")
        tier = _tier_from_variant(variant)
        customer = cust.Customer(
            customer_id=str(uuid.uuid4())[:8],
            name=data.get("full_name", email.split("@")[0]),
            email=email, tier=tier,
            purchased_at=date.today().isoformat(),
            source="gumroad",
            gumroad_subscription_id=subscription_id,
            license_key=data.get("license_key", ""),
        )
        cust.add_customer(customer)
        return {"status": "customer_created", "tier": tier.value}

    # ── Existing subscriber, status change ───────────────────────────
    if existing:
        if is_cancelled or is_refunded or is_ended:
            cust.update_sub_status(existing.customer_id, cust.SubStatus.CANCELLED)
            return {"status": "marked_cancelled"}
        # renewal ping with no cancel flag -> confirm still active
        cust.update_sub_status(existing.customer_id, cust.SubStatus.ACTIVE)
        return {"status": "renewed_active"}

    return {"status": "ignored", "reason": "no matching customer, no new sale"}
