"""
Appello — PhonePe payment adapter.

One provider behind one interface. The voice pipeline never talks to a payment
gateway directly; it calls `create_checkout` to get a link it can read out, and
the webhook route calls `verify_webhook` and `order_status` to decide whether
money actually moved.

Three rules this module exists to enforce:

1. **Card data never reaches this service.** We hand the caller a hosted
   checkout URL and nothing else. Nothing here sees a PAN, and nothing here
   should ever be changed so that it does.

2. **A client-side callback is a hint, never proof.** The browser telling us
   "checkout closed" is unauthenticated and can simply be wrong — the caller may
   have closed the tab, or the message may be forged. `order_status` re-asks the
   provider, server to server, and that answer is the one that counts.

3. **A configuration or network fault is not a declined payment.** Both look
   identical to a caller mid-call, and an agent that says "your payment failed"
   because a token request timed out is worse than one that says nothing.
   Everything in here raises `PaymentUnavailable` for that case, distinct from a
   payment the provider actually rejected.

Configuration is entirely environmental — see .env.example. With
PHONEPE_CLIENT_ID unset the module reports `is_configured() == False` and the
tool layer declines to offer payment at all, rather than failing mid-sentence.
"""

import base64
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger("appello")


class PaymentUnavailable(Exception):
    """The gateway could not be reached or is misconfigured.

    Deliberately distinct from a payment the provider declined: the first is our
    problem and the agent should say so plainly, the second is the caller's and
    the agent should offer to retry.
    """


# ─── Configuration ───────────────────────────────────────────────────────
PHONEPE_ENV = os.getenv("PHONEPE_ENV", "sandbox").strip().lower()
CLIENT_ID = os.getenv("PHONEPE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("PHONEPE_CLIENT_SECRET", "").strip()
CLIENT_VERSION = os.getenv("PHONEPE_CLIENT_VERSION", "1").strip()
MERCHANT_ID = os.getenv("PHONEPE_MERCHANT_ID", "").strip()

# Where the provider should send its server-to-server webhook. Must be publicly
# reachable, so on Azure this is the App Service hostname.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# PhonePe's own published endpoints. Sandbox moves no real money, which is the
# only mode a demo should ever run in.
_ENDPOINTS = {
    "sandbox": {
        "token": "https://api-preprod.phonepe.com/apis/pg-sandbox/v1/oauth/token",
        "api": "https://api-preprod.phonepe.com/apis/pg-sandbox",
    },
    "production": {
        "token": "https://api.phonepe.com/apis/identity-manager/v1/oauth/token",
        "api": "https://api.phonepe.com/apis/pg",
    },
}

# How long a checkout link stays live. Long enough for someone to find their
# phone and their card, short enough that an abandoned call does not leave a
# payable link lying around.
CHECKOUT_TTL_S = int(os.getenv("PHONEPE_CHECKOUT_TTL_S", "900"))

_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0.0}


def is_configured() -> bool:
    """Can we transact at all? Checked before the agent offers to take payment."""
    return bool(CLIENT_ID and CLIENT_SECRET and PUBLIC_BASE_URL)


def _endpoints() -> Dict[str, str]:
    return _ENDPOINTS.get(PHONEPE_ENV, _ENDPOINTS["sandbox"])


def webhook_url() -> str:
    return f"{PUBLIC_BASE_URL}/webhooks/phonepe"


# ─── Auth ────────────────────────────────────────────────────────────────
async def _access_token(session: aiohttp.ClientSession) -> str:
    """OAuth client-credentials token, cached until a minute before it expires.

    The cache matters more than it looks: a token request in front of every
    checkout would put an extra round trip inside a live call, where the caller
    is already waiting in silence.
    """
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]

    form = {
        "client_id": CLIENT_ID,
        "client_version": CLIENT_VERSION,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
    try:
        async with session.post(
            _endpoints()["token"],
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                # Never log the body: it echoes credentials back on some errors.
                logger.error(f"[payments] auth failed, status {resp.status}")
                raise PaymentUnavailable("payment gateway authentication failed")
    except aiohttp.ClientError as e:
        raise PaymentUnavailable(f"payment gateway unreachable: {e}") from e

    token = body.get("access_token")
    if not token:
        raise PaymentUnavailable("payment gateway returned no access token")

    _token_cache["access_token"] = token
    # expires_at is absolute epoch seconds; fall back to a conservative window.
    _token_cache["expires_at"] = float(body.get("expires_at") or (now + 600))
    return token


# ─── Checkout ────────────────────────────────────────────────────────────
async def create_checkout(
    amount_rupees: float,
    description: str,
    session_id: str,
) -> Dict[str, Any]:
    """Create a hosted checkout and return the URL to hand the caller.

    `merchant_order_id` is ours and is what every later signal is reconciled
    against — the webhook quotes it, the status re-check queries it, and the
    Redis mapping from it back to `session_id` is how a confirmation finds the
    call it belongs to.
    """
    if not is_configured():
        raise PaymentUnavailable("payment gateway is not configured")

    amount_paise = int(round(amount_rupees * 100))
    if amount_paise <= 0:
        raise ValueError("amount must be positive")

    merchant_order_id = f"appello-{uuid.uuid4().hex[:18]}"
    payload = {
        "merchantOrderId": merchant_order_id,
        "amount": amount_paise,
        "expireAfter": CHECKOUT_TTL_S,
        # Echoed back on the webhook, which is how a confirmation is traced to
        # the call that asked for it without a database round trip.
        "metaInfo": {"udf1": session_id},
        "paymentFlow": {
            "type": "PG_CHECKOUT",
            "message": description[:100],
            "merchantUrls": {"redirectUrl": webhook_url()},
        },
    }

    async with aiohttp.ClientSession() as session:
        token = await _access_token(session)
        try:
            async with session.post(
                f"{_endpoints()['api']}/checkout/v2/pay",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"O-Bearer {token}",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status not in (200, 201):
                    logger.error(f"[payments] checkout failed, status {resp.status}")
                    raise PaymentUnavailable("could not create a payment link")
        except aiohttp.ClientError as e:
            raise PaymentUnavailable(f"payment gateway unreachable: {e}") from e

    # V2 has shipped both a flat and a nested shape; accept either rather than
    # breaking a live call over a response envelope.
    data = body.get("data") or body
    url = data.get("redirectUrl") or data.get("url")
    if not url:
        logger.error(f"[payments] no checkout URL in response keys={list(body)}")
        raise PaymentUnavailable("payment gateway returned no checkout link")

    logger.info(f"[payments] checkout created {merchant_order_id} for session {session_id}")
    return {
        "merchant_order_id": merchant_order_id,
        "checkout_url": url,
        "amount_rupees": amount_rupees,
        "expires_in_s": CHECKOUT_TTL_S,
        "provider_order_id": data.get("orderId"),
    }


async def order_status(merchant_order_id: str) -> Dict[str, Any]:
    """Ask the provider what actually happened. This is the authoritative answer.

    Called both when a client-side callback claims success and when a webhook
    arrives, because neither is trusted on its own.
    """
    if not is_configured():
        raise PaymentUnavailable("payment gateway is not configured")

    async with aiohttp.ClientSession() as session:
        token = await _access_token(session)
        try:
            async with session.get(
                f"{_endpoints()['api']}/checkout/v2/order/{merchant_order_id}/status",
                params={"details": "false"},
                headers={"Authorization": f"O-Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    raise PaymentUnavailable(f"status check failed ({resp.status})")
        except aiohttp.ClientError as e:
            raise PaymentUnavailable(f"payment gateway unreachable: {e}") from e

    data = body.get("data") or body
    state = (data.get("state") or "").upper()
    return {
        "merchant_order_id": merchant_order_id,
        "state": state,
        "paid": state == "COMPLETED",
        "amount_paise": data.get("amount"),
    }


# ─── Webhook ─────────────────────────────────────────────────────────────
def verify_webhook(x_verify: str, response_b64: str) -> bool:
    """X-VERIFY = SHA256(response + salt) + '###' + saltIndex.

    Compared with `hmac.compare_digest` rather than `==` so the check does not
    leak the expected value through timing.
    """
    import hmac

    if not x_verify or not response_b64 or not CLIENT_SECRET:
        return False
    digest = hashlib.sha256((response_b64 + CLIENT_SECRET).encode()).hexdigest()
    expected = f"{digest}###{CLIENT_VERSION}"
    return hmac.compare_digest(x_verify.strip(), expected)


def decode_webhook(response_b64: str) -> Dict[str, Any]:
    """Decode the base64 payload. Raises on anything that is not valid JSON."""
    raw = base64.b64decode(response_b64)
    return json.loads(raw.decode("utf-8"))
