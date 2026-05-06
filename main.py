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
















 # Purpose: Shopping cart API — stores and manages cart data in Redis
# Tech stack: Flask + Redis + Gunicorn
# How the cart works: 
# 
# Every user gets a UUID session ID stored in a browser cookie
# Cart data is stored in Redis as a JSON blob with the key cart:{session_uuid}
# Cart automatically expires after 7 days (configurable via CART_TTL env var)
# No database needed — Redis handles everything in memory 

# Redis Key Structure:
# cart:a3f2c1d4-8b9e-4f2a-9c1d-3e5f7a8b2c4d
  # --> {  
        # "items": {
          # "1": { "name": "Wireless Mouse", "price": 29.99, "qty": 2 },
          # "3": { "name": "Keyboard",       "price": 89.99, "qty": 1 }
        # },
        # "total": 149.97
      # }
# All API Endpoints:
# MethodEndpointWhat it doesGET/cartReturns the current user's cartPOST/cart/itemsAdds or increases quantity of an itemDELETE/cart/items/{id}Removes one specific itemDELETE/cartClears the entire cart (called after checkout)GET/healthzLiveness probe — always returns 200 OKGET/readyzReadiness probe — returns 503 if Redis is downGET/metricsPrometheus metrics scrape endpoint
# POST /cart/items — Request body:
# json{
  # "product_id": 1,
  # "price": 29.99,
  # "name": "Wireless Mouse",
  # "qty": 2
# }
# Session Cookie — Security flags:
# pythonresp.set_cookie(
  # SESSION_COOKIE,
  # session_id,
  # httponly=True,    # JS cannot read this cookie -- prevents XSS theft
  # secure=True,      # Only sent over HTTPS -- never plain HTTP
  # samesite="Lax",   # Blocks cross-site request forgery (CSRF)
  # max_age=CART_TTL_SECS
# )
# 
# HttpOnly — malicious JavaScript cannot steal the session ID
# Secure — cookie never travels over unencrypted HTTP
# SameSite=Lax — browser only sends cookie for same-site requests
# 
# Readiness Probe Logic:
# python@app.get("/readyz")
# def ready():
    # try:
        # r = get_redis()
        # r.ping()              # Tries to reach Redis
        # return {"status": "ready"}
    # except Exception:
        # return {"status": "unavailable"}, 503  # Returns 503 if Redis is down
# 
# If Redis is down, Kubernetes stops sending traffic to this pod
# Pod stays up but is marked not ready — prevents requests from failing
# 
# Logging:
# python# Structured JSON format — CloudWatch and FluentBit can parse this
# {"time":"2024-01-15T10:30:00","level":"INFO","msg":"Added product 1 to cart a3f2c1..."}
# 
# Every cart operation is logged with session ID (truncated for privacy)
####  JSON format means CloudWatch can filter and search logs easily
# 
####  Prometheus Metrics:
#####  pythonmetrics = PrometheusMetrics(app, path="/metrics")

#####  Automatically tracks request count, latency, and error rate per endpoint
#####  Scraped by Prometheus every 30 seconds via the ServiceMonitor

##### Input Validation:
#####  pythonif not body or "product_id" not in body or "price" not in body:
  #####  return {"error": "product_id and price are required"}, 400

##### if qty < 1:
   ##### return {"error": "qty must be >= 1"}, 400
#####
##### Rejects bad requests with clear error messages
##### Prevents negative quantities or missing required fields

##### Total Recalculation:
#####  pythoncart["total"] = round(
   ##### sum(item["price"] * item["qty"] for item in cart["items"].values()), 2
##### )  #####

##### Total is always recalculated server-side on every save #####
 ##### Never trust the client to send the correct total   #####
####  Rounded to 2 decimal places to avoid floating point issues  #####
