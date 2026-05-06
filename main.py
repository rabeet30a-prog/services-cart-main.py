"""
cart-api: Shopping cart microservice
Flask + Redis — runs on port 8001

Cart data is stored in Redis with TTL (session expiry).
Key format: cart:{session_id}
"""

import os
import json
import uuid
import logging
from typing import Dict, Any

import redis
from flask import Flask, request, jsonify, make_response
from prometheus_flask_exporter import PrometheusMetrics

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
REDIS_URL      = os.environ["REDIS_URL"]       # Injected by K8s secret
CART_TTL_SECS  = int(os.getenv("CART_TTL", str(7 * 24 * 3600)))  # 7 days
SESSION_COOKIE = "cart_session"

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
metrics = PrometheusMetrics(app, path="/metrics")

# ── Redis Client ──────────────────────────────────────────────────────────────
def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)


def get_or_create_session(request) -> str:
    """Return existing session ID from cookie, or create a new one."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        session_id = str(uuid.uuid4())
    return session_id


def get_cart(r: redis.Redis, session_id: str) -> Dict[str, Any]:
    """Load cart from Redis; return empty cart if none."""
    raw = r.get(f"cart:{session_id}")
    if raw:
        return json.loads(raw)
    return {"items": {}, "total": 0.0}


def save_cart(r: redis.Redis, session_id: str, cart: Dict) -> None:
    """Persist cart to Redis with TTL reset."""
    # Recalculate total
    cart["total"] = round(
        sum(item["price"] * item["qty"] for item in cart["items"].values()), 2
    )
    r.setex(f"cart:{session_id}", CART_TTL_SECS, json.dumps(cart))


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/healthz")
def health():
    """Liveness probe"""
    return jsonify({"status": "ok"})


@app.get("/readyz")
def ready():
    """Readiness probe — checks Redis connectivity"""
    try:
        r = get_redis()
        r.ping()
        return jsonify({"status": "ready"})
    except Exception as e:
        logger.error(f"Readiness failed: {e}")
        return jsonify({"status": "unavailable"}), 503


@app.get("/cart")
def get_cart_route():
    """Return the current session's cart"""
    session_id = get_or_create_session(request)
    r = get_redis()
    cart = get_cart(r, session_id)

    resp = make_response(jsonify(cart))
    resp.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,   # SECURITY: Not accessible via JS
        secure=True,     # SECURITY: HTTPS only
        samesite="Lax",  # SECURITY: CSRF protection
        max_age=CART_TTL_SECS,
    )
    return resp


@app.post("/cart/items")
def add_item():
    """Add or update an item in the cart"""
    session_id = get_or_create_session(request)
    body = request.get_json()

    if not body or "product_id" not in body or "price" not in body:
        return jsonify({"error": "product_id and price are required"}), 400

    product_id = str(body["product_id"])
    qty        = int(body.get("qty", 1))
    price      = float(body["price"])
    name       = body.get("name", "Unknown Product")

    if qty < 1:
        return jsonify({"error": "qty must be >= 1"}), 400

    r    = get_redis()
    cart = get_cart(r, session_id)

    if product_id in cart["items"]:
        cart["items"][product_id]["qty"] += qty
    else:
        cart["items"][product_id] = {"name": name, "price": price, "qty": qty}

    save_cart(r, session_id, cart)
    logger.info(f"Added product {product_id} to cart {session_id[:8]}...")

    resp = make_response(jsonify(cart), 201)
    resp.set_cookie(SESSION_COOKIE, session_id, httponly=True, secure=True, samesite="Lax", max_age=CART_TTL_SECS)
    return resp


@app.delete("/cart/items/<product_id>")
def remove_item(product_id: str):
    """Remove an item from the cart"""
    session_id = get_or_create_session(request)
    r    = get_redis()
    cart = get_cart(r, session_id)

    if product_id not in cart["items"]:
        return jsonify({"error": "Item not in cart"}), 404

    del cart["items"][product_id]
    save_cart(r, session_id, cart)
    return jsonify(cart)


@app.delete("/cart")
def clear_cart():
    """Empty the entire cart (called after successful checkout)"""
    session_id = get_or_create_session(request)
    r = get_redis()
    r.delete(f"cart:{session_id}")
    return jsonify({"items": {}, "total": 0.0})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001, debug=False)
