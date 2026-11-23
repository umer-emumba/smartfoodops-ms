"""Seeds the demo catalog, accounts and history through the gateway.

Run after `docker compose up` (once all services report healthy):

    uv run --group dev python scripts/seed.py

See docs/architecture.md#conventions — this is what makes `docker compose up`
produce an immediately demoable system instead of an empty shell.

What it creates:
- accounts: a system_admin, a customer (with a Home address in Karachi),
  two restaurant admins and two approved riders;
- the catalog in scripts/fixtures/catalog.json: 10 restaurants in Islamabad
  and Karachi with their menus (dietary tags, spice level, calories).
  Demo Bistro belongs to restaurant-admin@ (the smoke test uses it); the
  rest belong to partner@;
- history (week4-plan step 1): the customer's past orders and ~200 reviews
  from past customers, each on its own delivered order, spread over the
  last 90 days.

Safe to re-run: accounts, the address, restaurants, categories and items
are reused rather than duplicated (and items updated to match the fixture,
stock topped back up, so a re-run also resets the demo after many smoke-test
orders); history uses fixed IDs, so nothing is imported twice.

Everything goes through the public API except two things there is no
endpoint for, which run as each service's own command through
`docker compose exec` (so this script needs the local Docker stack, not
just the gateway URL):
- the admin — user-service's app/create_admin.py;
- the history — order-service's app/import_history.py writes the delivered
  orders, then restaurant-service's app/import_reviews.py the reviews.
  The real saga can't backdate orders and takes minutes per order. These
  orders have no payments, deliveries or events, so analytics doesn't count
  them.
"""

import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "http://localhost:8080"
PASSWORD = "DemoPass123!"
REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG = Path(__file__).resolve().parent / "fixtures" / "catalog.json"
# user-service's app/create_admin.py: an admin already exists.
EXIT_ADMIN_EXISTS = 3

STOCK = 50
HOME = {"text": "45 Clifton Road, Karachi", "lat": 24.8138, "lng": 67.0300}
# Fixed namespace: the same fixture entry always gets the same order ID, which
# is what makes the history import re-runnable.
HISTORY_NS = uuid.UUID("5f0d6c1e-8a4b-4c39-9a57-1e2f3d4c5b6a")
# Past customers the fixture's reviews are spread over. Opaque IDs: they have
# no user-service account.
PAST_CUSTOMERS = 40


class WaitOutRateLimits(httpx.HTTPTransport):
    """Waits out the gateway's rate limits instead of failing: a full seed
    makes about a dozen auth calls (limit 10/min per IP) and ~170 catalog
    writes as one restaurant admin (120/min per user)."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for _ in range(5):
            response = super().handle_request(request)
            if response.status_code != 429:
                return response
            response.read()
            wait_ms = response.json()["error"]["details"].get("retry_after_ms", 60_000)
            print(f"  (rate-limited, waiting {wait_ms / 1000:.0f}s)")
            response.close()
            time.sleep(wait_ms / 1000 + 1)
        return response


def login(client: httpx.Client, email: str) -> str:
    resp = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    resp.raise_for_status()
    return str(resp.json()["access_token"])


def register_and_login(
    client: httpx.Client, *, email: str, role: str, full_name: str, phone_number: str
) -> str:
    resp = client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": full_name,
            "phone_number": phone_number,
            "role": role,
        },
    )
    if resp.status_code not in (201, 409):
        resp.raise_for_status()
    return login(client, email)


def create_admin_and_login(client: httpx.Client) -> str:
    email = "admin@demo.smartfoodops.example"
    result = subprocess.run(
        [
            *("docker", "compose", "exec", "-T", "user-service"),
            *("python", "-m", "app.create_admin", "--email", email),
            *("--full-name", "Demo System Admin", "--phone", "+923000000000"),
            "--password-stdin",  # never as an argument: it would show in `ps`
        ],
        input=PASSWORD,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
    )
    if result.returncode not in (0, EXIT_ADMIN_EXISTS):
        sys.exit(f"Seed failed: creating the admin: {result.stderr.strip()}")
    return login(client, email)


def ensure_home_address(client: httpx.Client, headers: dict) -> None:
    resp = client.get("/v1/users/me/addresses", headers=headers)
    resp.raise_for_status()
    if any(a["label"] == "Home" for a in resp.json()):
        return
    client.post(
        "/v1/users/me/addresses",
        headers=headers,
        json={
            "label": "Home",
            "line1": "45 Clifton Road",
            "city": "Karachi",
            "lat": 24.8138,
            "lng": 67.0300,
            "is_default": True,
        },
    ).raise_for_status()


def ensure_restaurant(
    client: httpx.Client, spec: dict[str, Any], headers: dict, admin_headers: dict
) -> dict:
    """Created on the first run; on later runs its profile is updated to
    match the fixture. Approved if it isn't active."""
    resp = client.get("/v1/restaurants/mine", headers=headers)
    resp.raise_for_status()
    restaurant = next((r for r in resp.json() if r["name"] == spec["name"]), None)
    profile = {
        "address": spec["address"],
        "lat": spec["lat"],
        "lng": spec["lng"],
        "contact_phone": spec["contact_phone"],
        "cuisine_type": spec["cuisine_type"],
        "description": spec["description"],
    }
    if restaurant is None:
        print(f"  creating {spec['name']}...")
        resp = client.post(
            "/v1/restaurants",
            headers=headers,
            json={"name": spec["name"], "city": spec["city"], **profile},
        )
        resp.raise_for_status()
        restaurant = resp.json()
    # Prep time isn't settable on create.
    resp = client.patch(
        f"/v1/restaurants/{restaurant['id']}",
        headers=headers,
        json={**profile, "avg_prep_time_minutes": spec["avg_prep_time_minutes"]},
    )
    resp.raise_for_status()
    if restaurant["status"] != "active":
        client.post(
            f"/v1/restaurants/{restaurant['id']}/approve", headers=admin_headers
        ).raise_for_status()
    return dict(resp.json())


def ensure_menu(
    client: httpx.Client, headers: dict, restaurant_id: str, menu: list[dict]
) -> dict[str, dict]:
    """Creates missing categories and items; existing items are updated to
    the fixture, topped back up to STOCK and made available again. Returns
    the menu by item name."""
    base = f"/v1/restaurants/{restaurant_id}/menu"
    resp = client.get(f"{base}/categories", headers=headers)
    resp.raise_for_status()
    categories = {c["name"]: c["id"] for c in resp.json()}
    resp = client.get(base, headers=headers)
    resp.raise_for_status()
    existing = {item["name"]: item for item in resp.json()}

    items: dict[str, dict] = {}
    for category in menu:
        if category["category"] not in categories:
            resp = client.post(
                f"{base}/categories", headers=headers, json={"name": category["category"]}
            )
            resp.raise_for_status()
            categories[category["category"]] = resp.json()["id"]
        for spec in category["items"]:
            fields = {
                "description": spec["description"],
                "price": spec["price"],
                "dietary_tags": spec["dietary_tags"],
                "spice_level": spec["spice_level"],
                "calories": spec["calories"],
                "stock_count": STOCK,
            }
            if spec["name"] in existing:
                resp = client.patch(
                    f"{base}/items/{existing[spec['name']]['id']}",
                    headers=headers,
                    json={**fields, "is_available": True},
                )
            else:
                resp = client.post(
                    f"{base}/items",
                    headers=headers,
                    json={
                        **fields,
                        "category_id": categories[category["category"]],
                        "name": spec["name"],
                    },
                )
            resp.raise_for_status()
            items[spec["name"]] = resp.json()
    return items


def _placed_at(days_ago: int, n: int) -> datetime:
    """Lunch or dinner time (Pakistan, UTC+5), `days_ago` days back."""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    hour_pkt = (12, 13, 14, 19, 20, 21, 22)[n % 7]
    return today - timedelta(days=days_ago, hours=5 - hour_pkt, minutes=(n * 17) % 60)


def build_history(
    catalog: dict, restaurants: dict[str, dict], menus: dict[str, dict], customer_id: str
) -> tuple[list[dict], list[dict]]:
    """The orders and reviews to import, from the fixture's reviews (each by a
    past customer, on a delivered order of the reviewed items) and the demo
    customer's own orders."""
    orders: list[dict] = []
    reviews: list[dict] = []

    def add(
        key: str, order_id: str, customer: str, address: dict, lines: list, days_ago: int, n: int
    ) -> datetime:
        restaurant = restaurants[key]
        placed_at = _placed_at(days_ago, n)
        orders.append(
            {
                "id": order_id,
                "customer_id": customer,
                "restaurant_id": restaurant["id"],
                "restaurant_admin_id": restaurant["admin_user_id"],
                "restaurant_lat": restaurant["lat"],
                "restaurant_lng": restaurant["lng"],
                "delivery_address_text": address["text"],
                "delivery_lat": address["lat"],
                "delivery_lng": address["lng"],
                "items": [
                    {
                        "menu_item_id": menus[key][name]["id"],
                        "name": name,
                        "quantity": quantity,
                        "unit_price": menus[key][name]["price"],
                    }
                    for name, quantity in lines
                ],
                "placed_at": placed_at.isoformat(),
            }
        )
        return placed_at

    def review(key: str, order_id: str, customer: str, spec: dict, placed_at: datetime) -> None:
        reviews.append(
            {
                "order_id": order_id,
                "customer_id": customer,
                "restaurant_id": restaurants[key]["id"],
                "rating": spec["rating"],
                "text": spec["text"],
                "item_ids": list(dict.fromkeys(menus[key][name]["id"] for name in spec["items"])),
                # A couple of hours after delivery.
                "created_at": (placed_at + timedelta(hours=2)).isoformat(),
            }
        )

    for n, spec in enumerate(catalog["reviews"]):
        key = spec["restaurant"]
        order_id = str(uuid.uuid5(HISTORY_NS, f"review:{n}"))
        customer = str(uuid.uuid5(HISTORY_NS, f"customer:{n % PAST_CUSTOMERS}"))
        # Somewhere in the restaurant's neighbourhood (~1–3 km away).
        offset = ((n % 5) - 2) * 0.008, ((n % 3) - 1) * 0.012 + 0.006
        restaurant = restaurants[key]
        address = {
            "text": f"House {10 + n}, {restaurant['city']}",
            "lat": restaurant["lat"] + offset[0],
            "lng": restaurant["lng"] + offset[1],
        }
        placed_at = add(
            key, order_id, customer, address, [(i, 1) for i in spec["items"]], spec["days_ago"], n
        )
        review(key, order_id, customer, spec, placed_at)

    for n, spec in enumerate(catalog["demo_customer_orders"]):
        key = spec["restaurant"]
        order_id = str(uuid.uuid5(HISTORY_NS, f"demo-order:{n}"))
        lines = [(i["name"], i["quantity"]) for i in spec["items"]]
        placed_at = add(key, order_id, customer_id, HOME, lines, spec["days_ago"], n)
        if spec["review"]:
            items = {"items": [i["name"] for i in spec["items"]]}
            review(key, order_id, customer_id, {**spec["review"], **items}, placed_at)
    return orders, reviews


def run_import(service: str, module: str, rows: list[dict]) -> None:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", service, "python", "-m", module],
        input=json.dumps(rows),
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        sys.exit(f"Seed failed: {service} {module}: {result.stderr.strip()}")
    print(f"  {service}: {result.stdout.strip()}")


def main() -> None:
    catalog = json.loads(CATALOG.read_text())
    with httpx.Client(base_url=BASE_URL, timeout=10.0, transport=WaitOutRateLimits()) as client:
        print("Creating demo system_admin...")
        admin_token = create_admin_and_login(client)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        print("Registering demo customer...")
        customer_token = register_and_login(
            client,
            email="customer@demo.smartfoodops.example",
            role="customer",
            full_name="Demo Customer",
            phone_number="+923000000001",
        )
        customer_headers = {"Authorization": f"Bearer {customer_token}"}
        ensure_home_address(client, customer_headers)
        resp = client.get("/v1/users/me", headers=customer_headers)
        resp.raise_for_status()
        customer_id = resp.json()["id"]

        print("Registering demo restaurant admins...")
        owners = {
            "demo": register_and_login(
                client,
                email="restaurant-admin@demo.smartfoodops.example",
                role="restaurant_admin",
                full_name="Demo Restaurant Admin",
                phone_number="+923000000002",
            ),
            "partner": register_and_login(
                client,
                email="partner@demo.smartfoodops.example",
                role="restaurant_admin",
                full_name="Demo Partner Restaurants",
                phone_number="+923000000005",
            ),
        }

        print(f"Making sure the catalog exists, stock at {STOCK}...")
        restaurants: dict[str, dict] = {}
        menus: dict[str, dict] = {}
        for spec in catalog["restaurants"]:
            headers = {"Authorization": f"Bearer {owners[spec['owner']]}"}
            restaurant = ensure_restaurant(client, spec, headers, admin_headers)
            restaurants[spec["key"]] = restaurant
            menus[spec["key"]] = ensure_menu(client, headers, restaurant["id"], spec["menu"])

        # Two riders: reassignment (decline, drop-out) needs someone to fall back to.
        for email, full_name, phone, vehicle in [
            ("rider@demo.smartfoodops.example", "Demo Rider", "+923000000003", "bike"),
            ("rider2@demo.smartfoodops.example", "Demo Rider 2", "+923000000004", "scooter"),
        ]:
            print(f"Registering and approving {email}...")
            rider_token = register_and_login(
                client, email=email, role="rider", full_name=full_name, phone_number=phone
            )
            rider_headers = {"Authorization": f"Bearer {rider_token}"}
            resp = client.post("/v1/riders", headers=rider_headers, json={"vehicle_type": vehicle})
            if resp.status_code not in (201, 409):
                resp.raise_for_status()
            rider_id = client.get("/v1/riders/me", headers=rider_headers).json()["id"]
            client.post(f"/v1/riders/{rider_id}/approve", headers=admin_headers).raise_for_status()

        print("Importing order history and reviews...")
        orders, reviews = build_history(catalog, restaurants, menus, customer_id)
        run_import("order-service", "app.import_history", orders)
        run_import("restaurant-service", "app.import_reviews", reviews)

        items = sum(len(m) for m in menus.values())
        print(f"\nDone. {len(restaurants)} restaurants, {items} menu items, all active.")
        print(f"Demo restaurant (Demo Bistro) id={restaurants['demo-bistro']['id']}")
        print("Demo accounts (password for all): " + PASSWORD)
        print("  admin@demo.smartfoodops.example (system_admin)")
        print("  customer@demo.smartfoodops.example")
        print("  restaurant-admin@demo.smartfoodops.example (Demo Bistro)")
        print("  partner@demo.smartfoodops.example (the other restaurants)")
        print("  rider@demo.smartfoodops.example, rider2@demo.smartfoodops.example (approved)")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(f"Seed failed: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        sys.exit(1)
