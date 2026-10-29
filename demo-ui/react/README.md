# React demo UI

A portal per role — customer, restaurant, rider, admin — for testing
SmartFoodOps by hand. Vite + React + TypeScript + Mantine. For testing and
demos only.

## Run

```bash
docker compose up -d                              # whole stack
uv run --group dev python scripts/seed.py         # once: demo accounts
cd demo-ui/react
npm install --legacy-peer-deps                    # once (npm 10.8 crashes on Vite 8's optional peers without it)
npm run dev                                       # http://localhost:5173
```

Pick a demo account on the login page. **Each browser tab keeps its own
login** (`sessionStorage`), so open one tab per role: customer, restaurant,
Rider 1, admin.

The dev server proxies `/v1` (REST and the live feeds) to the gateway at
`localhost:8080` (`vite.config.ts`), so the gateway needs no CORS entry for
this app.

**Local only.** The login page contains the seeded demo accounts' password
(the same dummy value as `scripts/seed.py`). The dev server listens on
`127.0.0.1`; never point it at a shared or real environment.

## What each portal does

| Portal | Features |
|---|---|
| Customer | Browse restaurants and menus, cart, saved addresses or typed coordinates, card approves/declines, promo code; order history; order page with live status, payment, delivery, ETAs, rider position, cancel |
| Restaurant | Live board (accept / reject new orders, mark food ready); all orders by status; menu categories and items (price, stock, availability); promotions; analytics; settings (accepting orders, prep time); create a restaurant |
| Rider | Create a rider profile; go on shift near a restaurant or at coordinates; live offers with a countdown (accept / decline); held delivery: ride to restaurant, picked up, ride to customer, delivered, drop out |
| Admin | All orders (open one to refund, mark delivered, cancel); users (block / unblock); restaurants and riders (approve / suspend); platform analytics |
| Everyone | Notifications (mark read); register a new customer, restaurant admin or rider |

## A full order

1. **Rider 1** tab: *Go on shift* (0.3 km from Demo Bistro).
2. **Customer** tab: open Demo Bistro, *Add* an item, *Place order*.
3. **Restaurant** tab → *Live orders*: *Accept*.
4. **Rider 1**: *Accept* the offer.
5. **Restaurant**: *Mark ready*.
6. **Rider 1**: *Ride to restaurant* → *Picked up* → *Ride to customer* → *Delivered*.
7. **Admin** → *Orders* → open the order → *Refund*.

The **Temporal UI** link on an order page shows the saga's steps.

## Files

| File | What |
|---|---|
| `src/config.ts` | Gateway path, Temporal UI URL, demo accounts |
| `src/api.ts` | Login, tokens (refreshed once on 401), `fetch` through the gateway |
| `src/hooks.ts` | `useLoad`, `usePaged` (cursor pages), `useFeed` (live feeds, `?access_token=`), `run` (toasts) |
| `src/ui.tsx` | Status badges, formatting |
| `src/pages/` | `Login`, `OrderDetail` (shared by customer, restaurant, admin), `Notifications`, `Analytics`, and a folder per portal |
