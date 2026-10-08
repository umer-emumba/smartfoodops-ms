"""End-to-end smoke test of the running system, through api-gateway.

One test for the whole platform, not one per service: per-service behaviour is
covered by each service's own tests (services/*/tests). This checks what only
shows up when everything runs together — routing, migrations, config,
service-to-service calls, Redis/Mongo connectivity.

    uv run --group dev python scripts/seed.py        # once: demo accounts + restaurant
    uv run --group dev python scripts/smoke_test.py  # black-box, API only
    uv run --group dev python scripts/smoke_test.py --deep  # + checks inside local Docker

By default it only uses the public API (works against any deployment via
--base-url). --deep also looks inside the local docker compose stack: every
service's /readyz and /livez, the Redis cache entry, how refresh tokens are
stored, and the Mongo location ping. Safe to re-run: the one menu change it
makes is reverted. Exits non-zero if any check fails.
"""

import argparse
import json
import subprocess
import sys
import time
import uuid
from typing import Any

import httpx

PASSWORD = "DemoPass123!"
CUSTOMER = "customer@demo.smartfoodops.example"
ADMIN = "admin@demo.smartfoodops.example"
RESTAURANT_ADMIN = "restaurant-admin@demo.smartfoodops.example"
RIDER = "rider@demo.smartfoodops.example"
SERVICES = ["user", "restaurant", "order", "delivery", "payment", "notification"]

_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: Any = "") -> None:
    _results.append((name, ok))
    suffix = f"  [{detail}]" if detail != "" and not ok else ""
    print(("  PASS  " if ok else "  FAIL  ") + name + suffix)


def section(title: str) -> None:
    print(f"\n{title}")


def compose(cmd: str) -> str:
    """Runs `docker compose exec -T ...` (--deep only)."""
    return subprocess.run(
        f"docker compose exec -T {cmd}", shell=True, capture_output=True, text=True
    ).stdout.strip()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: httpx.Client, email: str) -> dict[str, str]:
    """Waits out the login rate limit (10/min per IP) instead of failing —
    seed.py right before this can use most of a minute's budget."""
    for _ in range(3):
        r = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
        if r.status_code != 429:
            break
        wait_ms = r.json()["error"]["details"].get("retry_after_ms", 60_000)
        print(f"  (login rate-limited, waiting {wait_ms / 1000:.0f}s)")
        time.sleep(wait_ms / 1000 + 1)
    if r.status_code == 401:
        sys.exit(f"Login failed for {email} — run scripts/seed.py first.")
    r.raise_for_status()
    return r.json()


def run(base_url: str, deep: bool) -> None:
    with httpx.Client(base_url=base_url, timeout=15) as c:
        section("Health")
        check("gateway /livez", c.get("/livez").status_code == 200)
        check("gateway /readyz", c.get("/readyz").status_code == 200)
        if deep:
            for svc in SERVICES:
                codes = compose(
                    f'{svc}-service python -c "import urllib.request as u; '
                    "print(u.urlopen('http://localhost:8000/readyz').status, "
                    "u.urlopen('http://localhost:8000/livez').status)\""
                )
                check(f"{svc}-service /readyz and /livez", codes == "200 200", codes)

        section("Auth")
        customer = login(c, CUSTOMER)
        check("customer login", bool(customer.get("access_token")))
        rotated = c.post("/v1/auth/refresh", json={"refresh_token": customer["refresh_token"]})
        check("refresh rotates the token pair", rotated.status_code == 200, rotated.status_code)
        if deep:
            stored = compose(
                "user-db psql -U postgres -d user_db -tAc "
                '"select token_hash from refresh_tokens order by created_at desc limit 1"'
            )
            check("refresh token stored as SHA-256", len(stored) == 64, stored[:16])
        customer_h = bearer(rotated.json()["access_token"])
        admin_h = bearer(login(c, ADMIN)["access_token"])
        owner_h = bearer(login(c, RESTAURANT_ADMIN)["access_token"])
        rider_h = bearer(login(c, RIDER)["access_token"])
        check("unauthenticated private route -> 401", c.get("/v1/orders").status_code == 401)

        section("Restaurants: pagination and caching")
        page = c.get("/v1/restaurants", params={"limit": 1})
        body = page.json()
        check("list returns {items, next_cursor}", set(body) == {"items", "next_cursor"}, body)
        check(
            "public list is cacheable",
            page.headers.get("cache-control") == "public, max-age=30",
            page.headers.get("cache-control"),
        )
        if body["next_cursor"]:
            nxt = c.get("/v1/restaurants", params={"limit": 1, "cursor": body["next_cursor"]})
            repeated = {i["id"] for i in body["items"]} & {i["id"] for i in nxt.json()["items"]}
            check("next page has no repeats", nxt.status_code == 200 and not repeated)
        check("malformed cursor -> 422", c.get("/v1/restaurants?cursor=junk").status_code == 422)

        mine = c.get("/v1/restaurants/mine", headers=owner_h).json()
        active = [r for r in mine if r["status"] == "active"]
        if not active:
            sys.exit("No active demo restaurant — run scripts/seed.py first.")
        rid = active[0]["id"]
        menu_first = c.get(f"/v1/restaurants/{rid}/menu")
        menu_again = c.get(f"/v1/restaurants/{rid}/menu")
        check("menu served", menu_first.status_code == 200 and menu_first.json())
        check("menu repeat read is identical", menu_first.json() == menu_again.json())
        check(
            "menu is cacheable",
            menu_again.headers.get("cache-control") == "public, max-age=30",
            menu_again.headers.get("cache-control"),
        )
        if deep:
            cached = compose(f"redis redis-cli EXISTS restaurant-cache:v1:{rid}:menu")
            check("menu cached in Redis", cached == "1", cached)

        item = menu_first.json()[0]
        original_price = item["price"]
        bumped = round(original_price + 1, 2)
        item_url = f"/v1/restaurants/{rid}/menu/items/{item['id']}"
        try:
            upd = c.patch(item_url, headers=owner_h, json={"price": str(bumped)})
            check("owner updates a menu item", upd.status_code == 200, upd.status_code)
            fresh = next(
                i for i in c.get(f"/v1/restaurants/{rid}/menu").json() if i["id"] == item["id"]
            )
            check("edit visible immediately (cache invalidated)", fresh["price"] == bumped, fresh)
            check("money is a JSON number", isinstance(fresh["price"], int | float))

            section("Orders")
            key = str(uuid.uuid4())
            order_body = {
                "restaurant_id": rid,
                "delivery_address_text": "1 Smoke Test St",
                "delivery_lat": 24.86,
                "delivery_lng": 67.01,
                "items": [{"menu_item_id": item["id"], "quantity": 2}],
            }
            trace_id = f"smoke-{uuid.uuid4()}"
            placed = c.post(
                "/v1/orders",
                headers={**customer_h, "Idempotency-Key": key, "X-Request-ID": trace_id},
                json=order_body,
            )
            check("customer places an order", placed.status_code == 201, placed.text[:200])
            check(
                "response echoes the trace ID",
                placed.headers.get("x-request-id") == trace_id,
                placed.headers.get("x-request-id"),
            )
            if deep:
                # The order spans gateway -> order-service -> restaurant-service;
                # all three must have logged under the client's trace ID.
                logs = subprocess.run(
                    "docker compose logs --no-log-prefix --since 2m "
                    "api-gateway order-service restaurant-service",
                    shell=True,
                    capture_output=True,
                    text=True,
                ).stdout
                services_seen = {
                    json.loads(line)["service"]
                    for line in logs.splitlines()
                    if trace_id in line and line.startswith("{")
                }
                check(
                    "one trace ID across gateway, order and restaurant logs",
                    services_seen >= {"api-gateway", "order-service", "restaurant-service"},
                    sorted(services_seen),
                )
            if placed.status_code == 201:
                order = placed.json()
                check(
                    "priced server-side from the live menu",
                    abs(order["subtotal"] - bumped * 2) < 1e-9,
                    order["subtotal"],
                )
                replay = c.post(
                    "/v1/orders", headers={**customer_h, "Idempotency-Key": key}, json=order_body
                )
                check(
                    "same Idempotency-Key returns the same order",
                    replay.status_code == 200 and replay.json()["id"] == order["id"],
                )
                own = c.get(f"/v1/orders/{order['id']}", headers=customer_h)
                check("customer reads own order", own.status_code == 200)
        finally:
            c.patch(item_url, headers=owner_h, json={"price": str(original_price)})

        unknown = c.post(
            "/v1/orders",
            headers={**customer_h, "Idempotency-Key": str(uuid.uuid4())},
            json={**order_body, "items": [{"menu_item_id": "no-such-item", "quantity": 1}]},
        )
        check("unknown menu item -> 422", unknown.status_code == 422, unknown.status_code)
        mine_orders = c.get("/v1/orders", headers=customer_h, params={"limit": 1})
        check("customer order list is a page", "items" in mine_orders.json())
        admin_orders = c.get("/v1/orders/admin", headers=admin_h, params={"status": "created"})
        check(
            "admin order list filters by status",
            admin_orders.status_code == 200
            and all(o["status"] == "created" for o in admin_orders.json()["items"]),
        )
        check(
            "unknown order status -> 422",
            c.get("/v1/orders/admin", headers=admin_h, params={"status": "bogus"}).status_code
            == 422,
        )
        check(
            "customer can't use admin route -> 403",
            c.get("/v1/orders/admin", headers=customer_h).status_code == 403,
        )

        section("Users, riders, notifications")
        users = c.get("/v1/users", headers=admin_h, params={"q": "demo"})
        check("admin user search", users.status_code == 200 and users.json()["items"])
        literal = c.get("/v1/users", headers=admin_h, params={"q": "%%"})
        check("'%' in search is literal", literal.json()["items"] == [])
        check(
            "blank search term -> 422",
            c.get("/v1/users", headers=admin_h, params={"q": "   "}).status_code == 422,
        )
        lat, lng = 24.9, 67.1 + (uuid.uuid4().int % 1000) / 1e6  # unique per run
        loc = c.patch("/v1/riders/me/location", headers=rider_h, json={"lat": lat, "lng": lng})
        check("rider location update", loc.status_code == 200, loc.status_code)
        if deep:
            pings = compose(
                "mongo mongosh delivery_db --quiet --eval "
                f'"db.rider_location_pings.countDocuments({{lng: {lng}}})"'
            )
            check("location ping written to Mongo", pings == "1", pings)
        check(
            "admin rider list is a page",
            "items" in c.get("/v1/riders/admin", headers=admin_h).json(),
        )
        check(
            "notification list is a page",
            "items" in c.get("/v1/notifications", headers=customer_h).json(),
        )

        section("Input hardening")
        extra = c.post(
            "/v1/orders",
            headers={**customer_h, "Idempotency-Key": str(uuid.uuid4())},
            json={**order_body, "total_amount": "0.01"},
        )
        check("unknown field -> 422 (not ignored)", extra.status_code == 422, extra.status_code)
        too_big = c.post(
            "/v1/orders",
            headers={**customer_h, "Idempotency-Key": str(uuid.uuid4())},
            content=b"x" * (1_048_576 + 1),
        )
        check("body over 1 MB -> 413", too_big.status_code == 413, too_big.status_code)

        section("Gateway")
        check(
            "/internal routes not reachable",
            c.post(f"/internal/restaurants/{rid}/quote", json={"item_ids": ["x"]}).status_code
            == 404,
        )
        check("unknown resource -> 404", c.get("/v1/nope", headers=customer_h).status_code == 404)
        # Last: reusing a rotated refresh token revokes all of this user's sessions.
        reuse = c.post("/v1/auth/refresh", json={"refresh_token": customer["refresh_token"]})
        check("reused refresh token rejected", reuse.status_code == 401, reuse.status_code)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument(
        "--deep", action="store_true", help="also check inside the local docker compose stack"
    )
    args = parser.parse_args()
    try:
        run(args.base_url, args.deep)
    except httpx.ConnectError:
        sys.exit(f"Can't reach {args.base_url} — is `docker compose up` running?")
    failed = [name for name, ok in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
