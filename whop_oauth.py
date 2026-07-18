"""
NexGame Lite — Whop OAuth ("Sign in with Whop")
Kage Software · 2026

This is the fix for the dead end flagged in chat: a customer who buys on
Whop has no way to discover their login credentials, because Whop's own
license-key delivery isn't wired to anything that hands it to the buyer.
Sign in with Whop skips that entirely — the customer clicks a button,
approves on Whop, and lands back here already authenticated, with their
paid-access status checked server-side. No key to lose, nothing to type.

Standard OAuth 2.1 + PKCE + OIDC. Every URL and response shape below was
verified 2026-07-18 by fetching docs.whop.com/developer/guides/oauth.md
and docs.whop.com/api-reference/beta/users/check-user-access.md directly
— this replaces an earlier version of this file that had three unverified
best-guess URLs (flagged honestly at the time, now corrected against the
real docs rather than left as guesses):
    - AUTHORIZE_URL was guessed as https://whop.com/oauth — actually
      https://api.whop.com/oauth/authorize.
    - The token exchange was built as a form-encoded POST with a
      client_secret field — Whop's documented example is a JSON POST
      with no client_secret (PKCE is the security mechanism here, not
      a confidential-client secret in this particular call).
    - ACCESS_CHECK_URL was guessed as a memberships-list endpoint —
      the real endpoint is much simpler: GET /users/{id}/access/{resource_id},
      returning {has_access, access_level} directly. It does NOT return
      a membership ID, which is why this file matches an existing
      customer by email instead of whop_membership_id (see
      customers.get_by_email's docstring for why).

Uses `requests` (already a project dependency, already used throughout
ingest/balldontlie_provider.py) rather than pulling in a new async HTTP
library — every route below is a plain sync `def`, which FastAPI runs
in its threadpool automatically, so a blocking requests call here
doesn't stall the event loop.
"""

import base64
import datetime
import hashlib
import hmac
import json
import secrets
import time
import uuid
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import RedirectResponse

import auth
import config
import customers as cust

router = APIRouter()

AUTHORIZE_URL = "https://api.whop.com/oauth/authorize"
TOKEN_URL = "https://api.whop.com/oauth/token"
USERINFO_URL = "https://api.whop.com/oauth/userinfo"
# {id} = the user_ tag from userinfo's "sub" field.
# {resource_id} = config.WHOP_ACCESS_PASS_ID, which must be a Product ID
# (prod_...) per the confirmed API spec — find it on that product's page
# in your Whop dashboard, or via GET /api/v1/products.
ACCESS_CHECK_URL = "https://api.whop.com/api/v1/users/{id}/access/{resource_id}"

PKCE_COOKIE_NAME = "whop_oauth_pkce"
PKCE_COOKIE_TTL_SECONDS = 600   # 10 min — just needs to survive the
                                # redirect round trip to Whop and back


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign_pkce_payload(verifier: str, state: str, nonce: str) -> str:
    """Reuses auth.py's own HMAC signing so the PKCE verifier can ride in
    a plain cookie between the two legs of the redirect, without needing
    server-side session storage — same stateless pattern as auth.py's
    session tokens, just a much shorter TTL."""
    body = json.dumps({
        "verifier": verifier, "state": state, "nonce": nonce,
        "expires": int(time.time()) + PKCE_COOKIE_TTL_SECONDS,
    }).encode()
    b64_body = _b64url(body)
    signature = auth._sign(b64_body.encode())
    return f"{b64_body}.{signature}"


def _verify_pkce_payload(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    b64_body, signature = token.rsplit(".", 1)
    expected = auth._sign(b64_body.encode())
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        padded = b64_body + "=" * (-len(b64_body) % 4)
        body = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception:
        return None
    if body.get("expires", 0) < time.time():
        return None
    return body


@router.get("/api/auth/whop/start")
def whop_oauth_start(response: Response):
    """Step 1: send the customer to Whop to approve access, carrying a
    PKCE code_challenge so the callback leg can prove it's the same
    browser session that started this (standard OAuth 2.1 requirement,
    not optional under PKCE)."""
    verifier = _b64url(secrets.token_bytes(32))   # matches docs' randomString(32)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(16))
    # Required whenever scope includes 'openid' — binds the ID token
    # Whop returns to this specific login attempt, so a stolen/replayed
    # ID token from a different session can't be reused here.
    nonce = _b64url(secrets.token_bytes(16))

    # Properly URL-encoded via urlencode rather than raw f-string
    # interpolation — some OAuth servers are strict about redirect_uri
    # in particular arriving percent-encoded (its own '://' and '/'
    # characters can otherwise confuse a strict query-string parser).
    params = urlencode({
        "response_type": "code",
        "client_id": config.WHOP_CLIENT_ID,
        "redirect_uri": config.WHOP_OAUTH_REDIRECT_URI,
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    redirect = RedirectResponse(f"{AUTHORIZE_URL}?{params}")
    redirect.set_cookie(
        PKCE_COOKIE_NAME, _sign_pkce_payload(verifier, state, nonce),
        httponly=True, max_age=PKCE_COOKIE_TTL_SECONDS, samesite="lax",
    )
    return redirect


@router.get("/api/auth/callback/whop")
def whop_oauth_callback(request: Request, code: str = "", state: str = "",
                        error: str = "", error_description: str = ""):
    """Step 2: Whop sends the customer back here. Exchange the code for
    a token (JSON POST, no client_secret — PKCE's code_verifier is what
    proves this request came from the same client that started the
    flow), confirm they actually hold access to NexGame Lite's product
    (not just a Whop account — those are different things), find their
    NexGame Lite customer record by email, and log them in exactly like
    /api/login does today."""
    if error:
        detail = f"Whop declined: {error}"
        if error_description:
            detail += f" — {error_description}"
        raise HTTPException(400, detail)

    pkce_cookie = request.cookies.get(PKCE_COOKIE_NAME, "")
    pkce = _verify_pkce_payload(pkce_cookie)
    if not pkce or pkce.get("state") != state:
        raise HTTPException(400, "OAuth state mismatch — start over from "
                                 "/api/auth/whop/start")

    token_resp = requests.post(TOKEN_URL, json={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.WHOP_OAUTH_REDIRECT_URI,
        "client_id": config.WHOP_CLIENT_ID,
        "code_verifier": pkce["verifier"],
    }, timeout=10)
    if token_resp.status_code != 200:
        detail = token_resp.json().get("error_description", "") \
                if token_resp.headers.get("content-type", "").startswith(
                    "application/json") else ""
        raise HTTPException(502, f"Whop token exchange failed: {detail or token_resp.status_code}")
    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    # Nonce check — if Whop returned an id_token (standard for an
    # 'openid' scope request), confirm its nonce claim matches what we
    # generated in whop_oauth_start. NOTE: this decodes the JWT payload
    # without verifying its cryptographic signature (that would need
    # fetching and caching Whop's JWKS, meaningfully more scope than
    # this fix needs). That's an acceptable tradeoff here specifically
    # because the nonce isn't this flow's actual trust boundary — the
    # access_resp call a few lines below independently re-verifies
    # paid access directly against Whop's server using our own
    # WHOP_API_KEY, not by trusting any client-supplied token claim.
    # The nonce check below is defense-in-depth against a replayed
    # authorize request, not the thing standing between an attacker
    # and a paid account.
    id_token = token_data.get("id_token", "")
    if id_token and id_token.count(".") == 2:
        try:
            _, payload_b64, _ = id_token.split(".")
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            id_claims = json.loads(base64.urlsafe_b64decode(padded.encode()))
            if id_claims.get("nonce") != pkce.get("nonce"):
                raise HTTPException(400, "OAuth nonce mismatch — start "
                                         "over from /api/auth/whop/start")
        except HTTPException:
            raise
        except Exception:
            pass   # malformed/unparseable id_token — not fatal, since
                   # the access check below is the real trust boundary

    userinfo_resp = requests.get(
        USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"},
        timeout=10)
    if userinfo_resp.status_code != 200:
        raise HTTPException(502, "Whop userinfo lookup failed")
    userinfo = userinfo_resp.json()
    whop_user_tag = userinfo.get("sub", "")          # e.g. "user_xxxxx"
    email = userinfo.get("email", "")
    name = userinfo.get("preferred_username", "") or userinfo.get("name", "") \
          or (email.split("@")[0] if email else "")

    # Confirm they actually hold access to YOUR product — being logged
    # into Whop at all isn't the same as having paid for NexGame Lite.
    access_resp = requests.get(
        ACCESS_CHECK_URL.format(id=whop_user_tag,
                                resource_id=config.WHOP_ACCESS_PASS_ID),
        headers={"Authorization": f"Bearer {config.WHOP_API_KEY}"},
        timeout=10)
    access_data = access_resp.json() if access_resp.status_code == 200 else {}
    has_access = access_data.get("has_access", False)

    if not has_access:
        raise HTTPException(403, "No active NexGame Lite access found on "
                                 "this Whop account")

    existing = cust.get_by_email(email) if email else None

    if not existing:
        # Fallback path — normally the webhook already created this
        # record the moment they paid (see whop_webhook.py). This only
        # fires if OAuth login somehow beats the webhook there, e.g. a
        # delayed webhook delivery.
        customer = cust.Customer(
            customer_id=str(uuid.uuid4())[:8],
            name=name, email=email, tier=cust.Tier.MONTHLY,
            purchased_at=datetime.date.today().isoformat(),
            source="whop",
        )
        cust.add_customer(customer)
        existing = cust.get_by_email(email)

    # Access is confirmed live via the API call above, but the local
    # sub_status may still say something stale (e.g. the webhook hasn't
    # landed yet) — bring it in line now that we've independently
    # verified access, rather than trusting a possibly-stale DB value.
    if existing.sub_status != cust.SubStatus.ACTIVE:
        cust.update_sub_status(existing.customer_id, cust.SubStatus.ACTIVE)

    # True in the common case too — most customers' rows already exist
    # from whop_webhook.py's membership.activated handler at purchase
    # time, but THIS is their first-ever login, which is the actual
    # moment 'signed up' can honestly be tracked from a browser pixel.
    is_new_signup = cust.mark_first_login(existing.customer_id)

    token = auth.create_session_token(existing.customer_id)
    # whop.track('complete_registration') has to fire from JS in the
    # browser (the Whop pixel is client-side) — it can't fire from this
    # server-side redirect. Signal it via a one-shot query param instead;
    # index.html's own script checks for ?new_signup=1 on load, fires
    # the pixel event once, then strips the param via history.replaceState
    # so a page refresh or bookmark doesn't re-fire it.
    redirect = RedirectResponse("/?new_signup=1" if is_new_signup else "/")
    redirect.set_cookie(auth.SESSION_COOKIE_NAME, token, httponly=True,
                        max_age=auth.SESSION_TTL_SECONDS, samesite="lax")
    redirect.delete_cookie(PKCE_COOKIE_NAME)
    return redirect
