"""Seeds a demo restaurant + menu + two riders + customer through the gateway.

Run after `docker compose up` (once all services report healthy):

    uv run --group dev python scripts/seed.py

See docs/architecture.md#conventions — this is what makes `docker compose up`
produce an immediately demoable system instead of an empty shell.

Safe to re-run: existing accounts, the customer's address, the restaurant,
its menu and the riders are reused rather than duplicated, and the menu's
stock is topped back up — so a re-run also resets the demo after many
smoke-test orders.

Everything goes through the public API except the admin: there is no
endpoint that creates one, so it's made by user-service's own command
(app/create_admin.py) through `docker compose exec` — which is why this
script needs the local Docker stack, not just the gateway URL.
"""

import subprocess
import sys
from pathlib import Path

import httpx

BASE_URL = "http://localhost:8080"
PASSWORD = "DemoPass123!"
REPO_ROOT = Path(__file__).resolve().parent.parent
# user-service's app/create_admin.py: an admin already exists.
EXIT_ADMIN_EXISTS = 3

RESTAURANT_NAME = "Demo Bistro"
CATEGORY_NAME = "Mains"
MENU = [("Chicken Biryani", 8.50), ("Veggie Wrap", 6.00)]
STOCK = 50


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

    resp = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    resp.raise_for_status()
    return resp.json()["access_token"]


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

    resp = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    resp.raise_for_status()
    return resp.json()["access_token"]


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


def ensure_restaurant(client: httpx.Client, headers: dict, admin_headers: dict) -> str:
    """The owner's Demo Bistro, created on the first run and reused after."""
    resp = client.get("/v1/restaurants/mine", headers=headers)
    resp.raise_for_status()
    restaurant = next((r for r in resp.json() if r["name"] == RESTAURANT_NAME), None)
    if restaurant is None:
        print("Creating demo restaurant...")
        resp = client.post(
            "/v1/restaurants",
            headers=headers,
            json={
                "name": RESTAURANT_NAME,
                "address": "123 Main St",
                "city": "Karachi",
                "lat": 24.8600,
                "lng": 67.0100,
                "contact_phone": "+92210000000",
                "cuisine_type": "Pakistani",
            },
        )
        resp.raise_for_status()
        restaurant = resp.json()
    else:
        print("Reusing demo restaurant...")
    if restaurant["status"] != "active":
        print("Approving demo restaurant (system_admin)...")
        client.post(
            f"/v1/restaurants/{restaurant['id']}/approve", headers=admin_headers
        ).raise_for_status()
    return str(restaurant["id"])


def ensure_menu(client: httpx.Client, headers: dict, restaurant_id: str) -> None:
    """Creates the category and items that are missing; tops existing items
    back up to STOCK and makes them available again."""
    base = f"/v1/restaurants/{restaurant_id}/menu"
    resp = client.get(f"{base}/categories", headers=headers)
    resp.raise_for_status()
    category = next((c for c in resp.json() if c["name"] == CATEGORY_NAME), None)
    if category is None:
        resp = client.post(f"{base}/categories", headers=headers, json={"name": CATEGORY_NAME})
        resp.raise_for_status()
        category = resp.json()

    resp = client.get(base, headers=headers)
    resp.raise_for_status()
    existing = {item["name"]: item for item in resp.json()}
    for name, price in MENU:
        if name in existing:
            client.patch(
                f"{base}/items/{existing[name]['id']}",
                headers=headers,
                json={"stock_count": STOCK, "is_available": True},
            ).raise_for_status()
        else:
            client.post(
                f"{base}/items",
                headers=headers,
                json={
                    "category_id": category["id"],
                    "name": name,
                    "description": f"Demo item: {name}",
                    "price": price,
                    "stock_count": STOCK,
                },
            ).raise_for_status()


def main() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
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
        ensure_home_address(client, {"Authorization": f"Bearer {customer_token}"})

        print("Registering demo restaurant admin...")
        restaurant_admin_token = register_and_login(
            client,
            email="restaurant-admin@demo.smartfoodops.example",
            role="restaurant_admin",
            full_name="Demo Restaurant Admin",
            phone_number="+923000000002",
        )
        headers = {"Authorization": f"Bearer {restaurant_admin_token}"}

        restaurant_id = ensure_restaurant(client, headers, admin_headers)
        print(f"Making sure the menu exists, stock at {STOCK}...")
        ensure_menu(client, headers, restaurant_id)

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

        print(f"\nDone. Demo restaurant_id={restaurant_id}, approved and active.")
        print("Demo accounts (password for all): " + PASSWORD)
        print("  admin@demo.smartfoodops.example (system_admin)")
        print("  customer@demo.smartfoodops.example")
        print("  restaurant-admin@demo.smartfoodops.example")
        print("  rider@demo.smartfoodops.example, rider2@demo.smartfoodops.example (approved)")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(f"Seed failed: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        sys.exit(1)
