"""End-to-end smoke test of the running system, through api-gateway.

One test for the whole platform, not one per service: per-service behaviour is
covered by each service's own tests (services/*/tests). This checks what only
shows up when everything runs together — routing, migrations, config,
service-to-service calls, Redis/Mongo connectivity.

    uv run --group dev python scripts/seed.py        # once: demo accounts + restaurant
    uv run --group dev python scripts/smoke_test.py  # black-box, API only
    uv run --group dev python scripts/smoke_test.py --deep  # + checks inside local Docker
    uv run --group dev python scripts/smoke_test.py --lifecycle  # + whole orders

By default it only uses the public API (works against any deployment via
--base-url). --deep also looks inside the local docker compose stack: every
service's /readyz and /livez, the Redis cache entry, how refresh tokens are
stored, and the Mongo location ping. Safe to re-run: the one menu change it
makes is reverted. Exits non-zero if any check fails.

--lifecycle runs whole orders through the saga (docs/week2-plan.md step 8):
happy path with both live feeds, restaurant rejection, payment decline,
cancellation, rider decline / drop-out and reassignment, a race for the
last unit of stock, and a partial refund. Needs the second demo rider from
seed.py. Each run uses up 3 units of one menu item (the delivered orders);
everything else — riders, capacity, the stock changed for the race — is put
back, and any order a failed scenario left open is cancelled.

--events (docs/week3-plan.md step 6) runs one delivered and one cancelled
order, then checks what their events caused: the customer's, restaurant's
and rider's notifications (each once, all sent), the analytics counts, one
Jaeger trace across >= 8 services, and the outbox backlog back to 0. Local
stack only (reads order-db, Jaeger on :16686, Prometheus on :9090).
"""

import argparse
import json
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

PASSWORD = "DemoPass123!"
CUSTOMER = "customer@demo.smartfoodops.example"
ADMIN = "admin@demo.smartfoodops.example"
RESTAURANT_ADMIN = "restaurant-admin@demo.smartfoodops.example"
RIDER = "rider@demo.smartfoodops.example"
SERVICES = ["user", "restaurant", "order", "delivery", "payment", "notification", "analytics"]

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
                # Otherwise it holds stock until the 5-minute restaurant timeout.
                cancel = c.post(f"/v1/orders/{order['id']}/cancel", headers=customer_h, json={})
                check("customer cancels own order", cancel.status_code == 202, cancel.status_code)
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


# --- Lifecycle scenarios (--lifecycle) ---------------------------------------
# Whole orders through the saga: restaurant decision, rider offers, delivery,
# compensation and refunds, watched over the live feeds. Each scenario puts
# both riders on shift first (rider 1 nearest), so they don't depend on each
# other; anything left open at the end is cancelled.

RIDER_2 = "rider2@demo.smartfoodops.example"
DONE = {"delivered", "rejected", "cancelled"}


class _Timeout(Exception):
    pass


class _WaitOutRateLimit(httpx.HTTPTransport):
    """The scenarios poll, and the gateway allows 120 requests/min per user —
    a 429 here means "slow down", not a failed check."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        while True:
            resp = super().handle_request(request)
            if resp.status_code != 429 or request.url.path.startswith("/v1/auth/"):
                return resp
            resp.read()
            wait_ms = resp.json()["error"]["details"].get("retry_after_ms", 5_000)
            time.sleep(wait_ms / 1000 + 0.1)


class _Stream:
    """Reads one SSE stream on a background thread, collecting (event, data)."""

    def __init__(self, base_url: str, path: str, headers: dict[str, str] | None = None) -> None:
        self.events: list[tuple[str, Any]] = []
        self._client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(10, read=None))
        self._thread = threading.Thread(target=self._read, args=(path, headers), daemon=True)
        self._thread.start()

    def _read(self, path: str, headers: dict[str, str] | None) -> None:
        name = "message"
        try:
            with self._client.stream("GET", path, headers=headers) as resp:
                self.events.append(("http", resp.status_code))
                for line in resp.iter_lines():
                    if line.startswith("event: "):
                        name = line.removeprefix("event: ")
                    elif line.startswith("data: "):
                        self.events.append((name, json.loads(line.removeprefix("data: "))))
        except httpx.HTTPError:
            pass  # closed by close()

    def of(self, name: str) -> list[Any]:
        return [data for event, data in self.events if event == name]

    def ended(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def close(self) -> None:
        self._client.close()


class _Lifecycle:
    def __init__(self, c: httpx.Client, base_url: str, deep: bool) -> None:
        self.c, self.base_url, self.deep = c, base_url, deep
        self.placed: list[str] = []
        self.owner_token = login(c, RESTAURANT_ADMIN)["access_token"]
        self.customer = bearer(login(c, CUSTOMER)["access_token"])
        self.owner = bearer(self.owner_token)
        self.admin = bearer(login(c, ADMIN)["access_token"])
        self.rider1 = bearer(login(c, RIDER)["access_token"])
        self.rider2 = bearer(login(c, RIDER_2)["access_token"])
        self.rider1_id = c.get("/v1/riders/me", headers=self.rider1).json()["id"]
        self.rider2_id = c.get("/v1/riders/me", headers=self.rider2).json()["id"]
        mine = c.get("/v1/restaurants/mine", headers=self.owner).json()
        self.rest = next(r for r in mine if r["status"] == "active")
        menu = c.get(f"/v1/restaurants/{self.rest['id']}/menu").json()
        self.item = next(i for i in menu if i["is_available"] and i["stock_count"])

    # --- reads ---------------------------------------------------------------
    def order(self, oid: str) -> dict[str, Any]:
        return self.c.get(f"/v1/orders/{oid}", headers=self.customer).json()

    def payment(self, oid: str) -> dict[str, Any]:
        return self.c.get(f"/v1/payments/by-order/{oid}", headers=self.customer).json()

    def delivery(self, oid: str) -> dict[str, Any]:
        return self.c.get(f"/v1/deliveries/by-order/{oid}", headers=self.customer).json()

    def stock(self) -> int:
        menu = self.c.get(f"/v1/restaurants/{self.rest['id']}/menu").json()
        return next(i["stock_count"] for i in menu if i["id"] == self.item["id"])

    def capacity_used(self) -> str:
        return compose(
            "restaurant-db psql -U postgres -d restaurant_db -tAc "
            f"\"select active_order_count from restaurants where id='{self.rest['id']}'\""
        )

    def rider_status(self, rider: dict[str, str]) -> str:
        return self.c.get("/v1/riders/me", headers=rider).json()["status"]

    def wait(self, fn: Callable[[], Any], label: str, timeout: float = 30) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if value := fn():
                return value
            time.sleep(0.5)
        raise _Timeout(f"timed out waiting for {label}")

    def wait_status(self, oid: str, status: str) -> dict[str, Any]:
        return self.wait(lambda: (o := self.order(oid))["status"] == status and o, status)

    def offer(self, rider: dict[str, str]) -> dict[str, Any]:
        def poll() -> Any:
            r = self.c.get("/v1/riders/me/offer", headers=rider)
            return r.json() if r.status_code == 200 else None

        return self.wait(poll, "a rider offer")

    # --- actions -------------------------------------------------------------
    def on_shift(self) -> None:
        """Rider 1 at 0.3 km from the restaurant, rider 2 at 1 km."""
        for rider, km in ((self.rider1, 0.3), (self.rider2, 1.0)):
            self.c.patch("/v1/riders/me/status", headers=rider, json={"status": "available"})
            self.move(rider, km)

    def place(self, payment_method: str = "card_simulated") -> str:
        r = self.c.post(
            "/v1/orders",
            headers={**self.customer, "Idempotency-Key": str(uuid.uuid4())},
            json={
                "restaurant_id": self.rest["id"],
                "delivery_address_text": "1 Lifecycle Lane",
                "delivery_lat": self.rest["lat"] + 0.02,
                "delivery_lng": self.rest["lng"] + 0.01,
                "items": [{"menu_item_id": self.item["id"], "quantity": 1}],
                "payment_method": payment_method,
            },
        )
        if r.status_code != 201:
            raise _Timeout(f"order not placed: {r.status_code} {r.text[:200]}")
        self.placed.append(r.json()["id"])
        return r.json()["id"]

    def accepted_order(self) -> str:
        oid = self.place()
        self.wait_status(oid, "awaiting_restaurant_acceptance")
        self.respond(oid, "accept")
        return oid

    def respond(self, oid: str, decision: str) -> None:
        body = {"decision": decision} | (
            {"reason": "Kitchen closed"} if decision == "reject" else {}
        )
        self.c.post(f"/v1/orders/{oid}/restaurant-response", headers=self.owner, json=body)

    def rider_act(self, rider: dict[str, str], delivery_id: str, action: str) -> None:
        body = {"reason": "Flat tyre"} if action == "drop-out" else None
        r = self.c.post(
            f"/v1/riders/me/deliveries/{delivery_id}/{action}", headers=rider, json=body
        )
        if r.is_error:
            raise _Timeout(f"rider {action}: {r.status_code} {r.text[:200]}")

    def move(self, rider: dict[str, str], km: float) -> None:
        """A location ping `km` north of the restaurant."""
        self.c.patch(
            "/v1/riders/me/location",
            headers=rider,
            json={"lat": self.rest["lat"] + km / 111.2, "lng": self.rest["lng"]},
        )

    def deliver(self, rider: dict[str, str], oid: str, delivery_id: str) -> dict[str, Any]:
        self.move(rider, 0.1)
        self.rider_act(rider, delivery_id, "picked-up")
        self.wait_status(oid, "picked_up")
        self.move(rider, 1.0)
        self.rider_act(rider, delivery_id, "delivered")
        return self.wait_status(oid, "delivered")

    # --- scenarios -----------------------------------------------------------
    def run(self) -> None:
        happy = self.scenario("1+9. Happy path, watched on both live feeds", self.happy_path)
        self.scenario("2. Restaurant rejects", self.restaurant_rejects)
        self.scenario("3. Payment declined", self.payment_declined)
        self.scenario("4. Customer cancels after a rider is assigned", self.cancel_assigned)
        self.scenario("5. Nearest rider declines, next rider delivers", self.rider_declines)
        self.scenario("6. Two orders race for the last unit", self.last_unit_race)
        self.scenario("7. Rider drops out after accepting, replaced", self.rider_drops_out)
        if happy:
            self.scenario("8. Partial refund after delivery", lambda: self.refund(happy))

    def scenario(self, title: str, fn: Callable[[], Any]) -> Any:
        section(title)
        self.on_shift()
        try:
            return fn()
        except (_Timeout, httpx.HTTPError, KeyError, StopIteration) as exc:
            check(f"scenario completed ({type(exc).__name__}: {exc})", False)
            return None

    def happy_path(self) -> str:
        stock, used = self.stock(), self.capacity_used() if self.deep else ""
        feed = _Stream(
            self.base_url,
            f"/v1/tracking/restaurants/{self.rest['id']}?access_token={self.owner_token}",
        )
        try:
            self.wait(lambda: feed.of("snapshot"), "restaurant feed snapshot")
            oid = self.place()
            track = _Stream(self.base_url, f"/v1/tracking/orders/{oid}", self.customer)
            new = self.wait(
                lambda: [o for o in feed.of("new_order") if o["order_id"] == oid], "new_order"
            )
            check("restaurant feed (token in URL) pushed the new order", bool(new))
        finally:
            feed.close()
        self.wait_status(oid, "awaiting_restaurant_acceptance")
        self.respond(oid, "accept")
        offer = self.offer(self.rider1)
        check(
            "nearest rider offered first",
            abs(offer["distance_to_pickup_km"] - 0.3) < 0.05,
            offer["distance_to_pickup_km"],
        )
        self.rider_act(self.rider1, offer["delivery_id"], "accept")
        self.wait_status(oid, "assigned")
        d = self.delivery(oid)
        check("pickup and dropoff ETAs set", bool(d["pickup_eta"] and d["dropoff_eta"]), d)
        self.deliver(self.rider1, oid, offer["delivery_id"])

        check("customer's stream closed itself on delivered", track.ended(15))
        seen = [s["status"] for s in track.of("snapshot") + track.of("status")]
        seen = [s for i, s in enumerate(seen) if s != "created" and (i == 0 or s != seen[i - 1])]
        expected = ["awaiting_restaurant_acceptance", "confirmed", "assigned", "picked_up"]
        check("customer saw every status in order", seen == [*expected, "delivered"], seen)
        check("customer got rider locations", bool(track.of("location")), len(track.of("location")))
        check("payment captured", self.payment(oid)["status"] == "captured", self.payment(oid))
        check("stock decremented once", self.stock() == stock - 1, (stock, self.stock()))
        if self.deep:
            check("capacity released", self.capacity_used() == used, (used, self.capacity_used()))
        check("rider available again", self.rider_status(self.rider1) == "available")
        return oid

    def restaurant_rejects(self) -> None:
        stock = self.stock()
        oid = self.place()
        self.wait_status(oid, "awaiting_restaurant_acceptance")
        self.respond(oid, "reject")
        o = self.wait_status(oid, "rejected")
        reason = o["status_reason"]
        check(
            "rejected with the restaurant's reason",
            reason == "restaurant_rejected: Kitchen closed",
            reason,
        )
        check("payment voided", self.payment(oid)["status"] == "voided", self.payment(oid))
        check("stock restored", self.wait(lambda: self.stock() == stock, "stock", 10))

    def payment_declined(self) -> None:
        stock = self.stock()
        oid = self.place("card_simulated_decline")
        o = self.wait_status(oid, "rejected")
        check(
            "rejected as payment_declined",
            o["status_reason"] == "payment_declined",
            o["status_reason"],
        )
        check("payment declined", self.payment(oid)["status"] == "declined", self.payment(oid))
        check("stock restored", self.wait(lambda: self.stock() == stock, "stock", 10))

    def cancel_assigned(self) -> None:
        stock = self.stock()
        oid = self.accepted_order()
        offer = self.offer(self.rider1)
        self.rider_act(self.rider1, offer["delivery_id"], "accept")
        self.wait_status(oid, "assigned")
        r = self.c.post(f"/v1/orders/{oid}/cancel", headers=self.customer, json={"reason": "Late"})
        check("cancel accepted", r.status_code == 202, r.status_code)
        self.wait_status(oid, "cancelled")
        check("payment voided", self.payment(oid)["status"] == "voided", self.payment(oid))
        check("delivery cancelled", self.delivery(oid)["status"] == "cancelled")
        check("stock restored", self.wait(lambda: self.stock() == stock, "stock", 10))
        check(
            "rider available again",
            self.wait(lambda: self.rider_status(self.rider1) == "available", "rider", 10),
        )

    def rider_declines(self) -> None:
        oid = self.accepted_order()
        first = self.offer(self.rider1)
        self.rider_act(self.rider1, first["delivery_id"], "decline")
        second = self.offer(self.rider2)
        check("same delivery offered to rider 2", second["delivery_id"] == first["delivery_id"])
        self.rider_act(self.rider2, second["delivery_id"], "accept")
        self.wait_status(oid, "assigned")
        self.deliver(self.rider2, oid, second["delivery_id"])
        d = self.delivery(oid)
        check(
            "delivered by rider 2, reassignment_count 1",
            d["rider_id"] == self.rider2_id and d["reassignment_count"] == 1,
            d,
        )
        check("payment captured", self.payment(oid)["status"] == "captured")

    def last_unit_race(self) -> None:
        item_url = f"/v1/restaurants/{self.rest['id']}/menu/items/{self.item['id']}"
        original = self.stock()
        self.c.patch(item_url, headers=self.owner, json={"stock_count": 1}).raise_for_status()
        try:
            gate = threading.Barrier(2)

            def racer() -> str:
                gate.wait()
                return self.place()

            with ThreadPoolExecutor(2) as pool:
                oids = list(pool.map(lambda _: racer(), range(2)))

            def settled() -> Any:
                states = {oid: self.order(oid) for oid in oids}
                busy = {"created"}
                return None if any(o["status"] in busy for o in states.values()) else states

            states = self.wait(settled, "both orders past reservation")
            got = sorted(o["status"] for o in states.values())
            check(
                "one proceeds, one rejected",
                got == ["awaiting_restaurant_acceptance", "rejected"],
                got,
            )
            loser: dict[str, Any] = next(
                (o for o in states.values() if o["status"] == "rejected"), {}
            )
            reason = loser.get("status_reason")
            check("loser rejected as out_of_stock", reason == "out_of_stock", reason)
            check("stock at 0, not negative", self.stock() == 0, self.stock())
            winner = next((oid for oid, o in states.items() if o["status"] != "rejected"), None)
            if winner:
                self.respond(winner, "reject")
                self.wait_status(winner, "rejected")
        finally:
            self.c.patch(item_url, headers=self.owner, json={"stock_count": original})

    def rider_drops_out(self) -> None:
        oid = self.accepted_order()
        offer = self.offer(self.rider1)
        self.rider_act(self.rider1, offer["delivery_id"], "accept")
        self.wait_status(oid, "assigned")
        self.rider_act(self.rider1, offer["delivery_id"], "drop-out")
        replacement = self.offer(self.rider2)
        check("delivery offered to rider 2", replacement["delivery_id"] == offer["delivery_id"])
        check("dropped rider goes offline", self.rider_status(self.rider1) == "offline")
        self.rider_act(self.rider2, replacement["delivery_id"], "accept")
        self.wait(lambda: self.delivery(oid)["status"] == "assigned", "rider 2 assigned")
        self.deliver(self.rider2, oid, replacement["delivery_id"])
        check(
            "delivered by the replacement, payment captured",
            self.payment(oid)["status"] == "captured",
        )

    def refund(self, oid: str) -> None:
        r = self.c.post(
            f"/v1/orders/{oid}/refund",
            headers=self.admin,
            json={"amount": "5.00", "reason": "Cold"},
        )
        check("admin refund accepted", r.status_code == 202, r.status_code)
        o = self.wait(lambda: (o := self.order(oid))["refunded_at"] and o, "refund recorded")
        p = self.payment(oid)
        check(
            "order refunded_amount 5.00, still delivered",
            o["refunded_amount"] == 5.0 and o["status"] == "delivered",
            o,
        )
        check("payment refunded_amount matches", p["refunded_amount"] == 5.0, p)
        if self.deep:
            rows = compose(
                'payment-db psql -U postgres -d payment_db -tAc "select count(*) from outbox_events'
                f" where event_type='RefundProcessed' and payload->>'order_id'='{oid}'\""
            )
            check("one RefundProcessed outbox row", rows == "1", rows)
        again = self.c.post(f"/v1/orders/{oid}/refund", headers=self.admin, json={"reason": "x"})
        check("second refund -> 409", again.status_code == 409, again.status_code)

    def clean_up(self) -> None:
        """Ends anything a failed scenario left open, and puts riders back."""
        for oid in self.placed:
            status = self.order(oid)["status"]
            if status in ("created", "awaiting_restaurant_acceptance", "confirmed", "assigned"):
                self.c.post(f"/v1/orders/{oid}/cancel", headers=self.customer, json={})
        self.on_shift()


def lifecycle(base_url: str, deep: bool) -> None:
    with httpx.Client(base_url=base_url, timeout=15, transport=_WaitOutRateLimit()) as c:
        lc = _Lifecycle(c, base_url, deep)
        try:
            lc.run()
        finally:
            lc.clean_up()


# --- Event pipeline (--events) -----------------------------------------------
# One delivered and one cancelled order, then what the events caused
# downstream (docs/week3-plan.md step 6): each person's notifications, the
# analytics counts, one trace in Jaeger, and the outboxes drained. Needs the
# local stack: it reads the trace ID from order-db and asks Jaeger and
# Prometheus directly.

JAEGER_URL = "http://localhost:16686"
_DEAD_LETTERED = 'sum(kafka_events_consumed_total{outcome="dead_lettered"})'
PROMETHEUS_URL = "http://localhost:9090"
_COUNTED = ("total_orders", "confirmed", "delivered", "cancelled")


class _Events(_Lifecycle):
    def run(self) -> None:
        before = self.steady_analytics()
        dead_before = self.prometheus(_DEAD_LETTERED)
        delivered = self.scenario("Delivered order", self.delivered_order)
        cancelled = self.scenario("Cancelled after a rider is assigned", self.cancelled_order)
        if not (delivered and cancelled):
            return

        section("Notifications (rows per channel, each once, all sent)")
        for role, user, oid, channels in (
            ("customer", self.customer, delivered, {"in_app": 5, "email": 2, "sms": 1}),
            ("restaurant_admin", self.owner, delivered, {"in_app": 2, "sms": 1}),
            ("rider", self.rider1, delivered, {"in_app": 2, "sms": 1}),
            ("customer", self.customer, cancelled, {"in_app": 4, "email": 2}),
            ("restaurant_admin", self.owner, cancelled, {"in_app": 2, "sms": 2}),
            ("rider", self.rider1, cancelled, {"in_app": 3, "sms": 2}),
        ):
            self.notified(
                role, user, oid, channels, "delivered" if oid == delivered else "cancelled"
            )

        section("Analytics")
        expected = {"total_orders": 2, "confirmed": 2, "delivered": 1, "cancelled": 1}

        def deltas() -> dict[str, int]:
            after = self.analytics()
            return {k: after[k] - before[k] for k in _COUNTED}

        got = self.settle(deltas, lambda d: d == expected, 30)
        check("summary counts moved by exactly these two orders", got == expected, got)

        section("Tracing and metrics")
        self.one_trace(delivered)
        backlog = self.settle(lambda: self.prometheus("sum(outbox_backlog)"), "0".__eq__, 30)
        check("outbox backlog back to 0 (Prometheus)", backlog == "0", backlog)
        dead_after = self.prometheus(_DEAD_LETTERED)
        check("nothing dead-lettered", dead_after == dead_before, (dead_before, dead_after))

    @staticmethod
    def settle[T](fn: Callable[[], T], ok: Callable[[T], bool], timeout: float) -> T:
        """Polls until `ok` or the timeout; returns the last value either way
        (consumers, sends and span export all lag a little)."""
        deadline = time.monotonic() + timeout
        value = fn()
        while not ok(value) and time.monotonic() < deadline:
            time.sleep(1)
            value = fn()
        return value

    def delivered_order(self) -> str:
        oid = self.accepted_order()
        offer = self.offer(self.rider1)
        self.rider_act(self.rider1, offer["delivery_id"], "accept")
        self.wait_status(oid, "assigned")
        self.deliver(self.rider1, oid, offer["delivery_id"])
        check("order delivered", True)
        return oid

    def cancelled_order(self) -> str:
        oid = self.accepted_order()
        offer = self.offer(self.rider1)
        self.rider_act(self.rider1, offer["delivery_id"], "accept")
        self.wait_status(oid, "assigned")
        self.c.post(f"/v1/orders/{oid}/cancel", headers=self.customer, json={"reason": "Late"})
        self.wait_status(oid, "cancelled")
        check("order cancelled", True)
        return oid

    def steady_analytics(self) -> dict[str, int]:
        """The counts once they stop moving: the basic run's order may still
        be on its way through the pipeline."""
        last = [self.analytics()]

        def changed() -> dict[str, int]:
            time.sleep(3)
            last.append(self.analytics())
            return last[-1]

        return self.settle(changed, lambda now: now == last[-2], 30)

    def analytics(self) -> dict[str, int]:
        r = self.c.get("/v1/analytics/summary", headers=self.admin)
        r.raise_for_status()
        return {k: r.json()["orders"][k] for k in _COUNTED}

    def notified(
        self, role: str, user: dict[str, str], oid: str, channels: dict[str, int], label: str
    ) -> None:
        """Every row the order's events made for this recipient, from
        notification-db (the API lists only in-app ones): the expected
        channels, none twice, all sent. Email/SMS fail at random
        (NOTIFY_FAKE_FAILURE_RATE) and are retried, so this waits."""
        who = f"{role}, {label}"

        def rows() -> list[dict[str, Any]]:
            out = compose(
                'notification-db psql -U postgres -d notification_db -tAc "select'
                " coalesce(json_agg(json_build_object('channel', channel, 'status', status,"
                " 'type', type, 'content', content)), '[]') from notifications"
                f" where related_order_id='{oid}' and recipient_role='{role}'\""
            )
            return json.loads(out or "[]")

        def settled(got: list[dict[str, Any]]) -> bool:
            pending = any(n["status"] == "pending" for n in got)
            return len(got) == sum(channels.values()) and not pending

        got = self.settle(rows, settled, 60)
        by_channel: dict[str, int] = {}
        for n in got:
            by_channel[n["channel"]] = by_channel.get(n["channel"], 0) + 1
        unique = {(n["type"], n["channel"], n["content"]) for n in got}
        check(f"{who}: {by_channel}", by_channel == channels, (channels, by_channel))
        check(f"{who}: no duplicates", len(unique) == len(got), len(got) - len(unique))
        statuses = sorted({n["status"] for n in got})
        check(f"{who}: all sent", statuses == ["sent"], statuses)
        page = self.c.get("/v1/notifications", headers=user, params={"limit": 100}).json()
        inbox = [n for n in page["items"] if n["related_order_id"] == oid]
        check(f"{who}: in their inbox", len(inbox) == channels["in_app"], len(inbox))

    def one_trace(self, oid: str) -> None:
        traceparent = compose(
            'order-db psql -U postgres -d order_db -tAc "select trace_context from outbox_events'
            f" where event_type='OrderPlaced' and aggregate_id='{oid}'\""
        )
        check("OrderPlaced outbox row carries a traceparent", traceparent.count("-") == 3)
        trace_id = traceparent.split("-")[1] if traceparent.count("-") == 3 else ""

        def services() -> set[str]:
            r = httpx.get(f"{JAEGER_URL}/api/v3/traces/{trace_id}", timeout=10)
            if r.status_code != 200:
                return set()
            return {
                a["value"]["stringValue"]
                for rs in r.json()["result"]["resourceSpans"]
                for a in rs["resource"]["attributes"]
                if a["key"] == "service.name"
            }

        seen = self.settle(services, lambda s: len(s) >= 8, 30)
        check(f"one trace spans >= 8 services ({len(seen)})", len(seen) >= 8, sorted(seen))

    @staticmethod
    def prometheus(query: str) -> str:
        r = httpx.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10)
        result = r.json()["data"]["result"]
        return f"{float(result[0]['value'][1]):g}" if result else "none"


def events(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=15, transport=_WaitOutRateLimit()) as c:
        ev = _Events(c, base_url, deep=True)
        try:
            ev.run()
        finally:
            ev.clean_up()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument(
        "--deep", action="store_true", help="also check inside the local docker compose stack"
    )
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="also run whole orders through the saga (about 2 minutes)",
    )
    parser.add_argument(
        "--events",
        action="store_true",
        help="also check notifications, analytics, tracing and metrics (local stack only)",
    )
    args = parser.parse_args()
    try:
        run(args.base_url, args.deep)
        if args.lifecycle:
            lifecycle(args.base_url, args.deep)
        if args.events:
            events(args.base_url)
    except httpx.ConnectError:
        sys.exit(f"Can't reach {args.base_url} — is `docker compose up` running?")
    failed = [name for name, ok in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
