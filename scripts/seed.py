"""Seeds a demo restaurant + menu + rider + customer through the gateway.

Run after `docker compose up` (once all services report healthy):

    uv run --group dev python scripts/seed.py

See docs/architecture.md#conventions — this is what makes `docker compose up`
produce an immediately demoable system instead of an empty shell.
"""

import sys

import httpx

BASE_URL = "http://localhost:8080"
PASSWORD = "DemoPass123!"
# Matches user-service's env.example ADMIN_BOOTSTRAP_SECRET dummy value.
BOOTSTRAP_SECRET = "dev-bootstrap-secret-change-me"


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


def bootstrap_admin_and_login(client: httpx.Client) -> str:
    email = "admin@demo.smartfoodops.local"
    resp = client.post(
        "/v1/auth/bootstrap-admin",
        headers={"X-Bootstrap-Secret": BOOTSTRAP_SECRET},
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": "Demo System Admin",
            "phone_number": "+923000000000",
        },
    )
    if resp.status_code not in (201, 409):
        resp.raise_for_status()

    resp = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    resp.raise_for_status()
    return resp.json()["access_token"]


def main() -> None:
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        print("Bootstrapping demo system_admin...")
        admin_token = bootstrap_admin_and_login(client)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        print("Registering demo customer...")
        customer_token = register_and_login(
            client,
            email="customer@demo.smartfoodops.local",
            role="customer",
            full_name="Demo Customer",
            phone_number="+923000000001",
        )
        client.post(
            "/v1/users/me/addresses",
            headers={"Authorization": f"Bearer {customer_token}"},
            json={
                "label": "Home",
                "line1": "45 Clifton Road",
                "city": "Karachi",
                "lat": 24.8138,
                "lng": 67.0300,
                "is_default": True,
            },
        )

        print("Registering demo restaurant admin...")
        restaurant_admin_token = register_and_login(
            client,
            email="restaurant-admin@demo.smartfoodops.local",
            role="restaurant_admin",
            full_name="Demo Restaurant Admin",
            phone_number="+923000000002",
        )
        headers = {"Authorization": f"Bearer {restaurant_admin_token}"}

        print("Creating demo restaurant...")
        resp = client.post(
            "/v1/restaurants",
            headers=headers,
            json={
                "name": "Demo Bistro",
                "address": "123 Main St",
                "city": "Karachi",
                "lat": 24.8600,
                "lng": 67.0100,
                "contact_phone": "+92210000000",
                "cuisine_type": "Pakistani",
            },
        )
        resp.raise_for_status()
        restaurant_id = resp.json()["id"]

        print("Approving demo restaurant (system_admin)...")
        client.post(
            f"/v1/restaurants/{restaurant_id}/approve", headers=admin_headers
        ).raise_for_status()

        print("Creating menu category + items...")
        resp = client.post(
            f"/v1/restaurants/{restaurant_id}/menu/categories",
            headers=headers,
            json={"name": "Mains"},
        )
        resp.raise_for_status()
        category_id = resp.json()["id"]

        for name, price in [("Chicken Biryani", 8.50), ("Veggie Wrap", 6.00)]:
            client.post(
                f"/v1/restaurants/{restaurant_id}/menu/items",
                headers=headers,
                json={
                    "category_id": category_id,
                    "name": name,
                    "description": f"Demo item: {name}",
                    "price": price,
                    "stock_count": 50,
                },
            ).raise_for_status()

        print("Registering demo rider...")
        rider_token = register_and_login(
            client,
            email="rider@demo.smartfoodops.local",
            role="rider",
            full_name="Demo Rider",
            phone_number="+923000000003",
        )
        rider_headers = {"Authorization": f"Bearer {rider_token}"}
        resp = client.post("/v1/riders", headers=rider_headers, json={"vehicle_type": "bike"})
        if resp.status_code not in (201, 409):
            resp.raise_for_status()
        rider_id = client.get("/v1/riders/me", headers=rider_headers).json()["id"]

        print("Approving demo rider (system_admin)...")
        client.post(f"/v1/riders/{rider_id}/approve", headers=admin_headers).raise_for_status()

        print(f"\nDone. Demo restaurant_id={restaurant_id}, approved and active.")
        print("Demo accounts (password for all): " + PASSWORD)
        print("  admin@demo.smartfoodops.local (system_admin)")
        print("  customer@demo.smartfoodops.local")
        print("  restaurant-admin@demo.smartfoodops.local")
        print("  rider@demo.smartfoodops.local (approved)")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(f"Seed failed: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        sys.exit(1)
