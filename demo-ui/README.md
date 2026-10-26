# Test UI

A plain HTML + JavaScript page for clicking through the order lifecycle by
hand: customer, restaurant, two riders and an admin side by side, with the
live feeds. For testing and demos only — no build step, no dependencies.

## Run

```bash
docker compose up -d                              # whole stack
uv run --group dev python scripts/seed.py         # once: demo accounts
cd demo-ui && python3 -m http.server 3000 --bind 127.0.0.1  # this machine only
```

Open http://localhost:3000 and click **Log in demo accounts**.

The page calls the gateway at `localhost:8080`, so the gateway must allow
this origin: `CORS_ORIGINS=http://localhost:3000` in
`services/api-gateway/.env` (the `env.example` default).

**Local only.** The page contains the seeded demo accounts' password (the same
dummy value as `scripts/seed.py`) and logs in as the system admin. Serve it on
`127.0.0.1` as above, and never point it at a shared or real environment.
Tokens live only in page memory (no `localStorage`, no cookies).

## A full order

1. **Rider 1** and **Rider 2**: *Go on shift* (0.3 km and 1 km from the restaurant).
2. **Customer**: pick an item, *Place order*. The restaurant panel shows it at once.
3. **Restaurant**: *Accept*. The order moves to *Preparing*.
4. **Rider 1** (nearest) gets the offer: *Accept*.
5. **Restaurant**: *Mark ready* — the customer and rider 1 are told, and the pickup ETA updates.
6. Rider 1: *Ride to restaurant* → *Picked up* → *Ride to customer* → *Delivered*.
7. The customer panel shows every status, the ETAs, food ready, rider positions, and the payment captured.
8. **Admin**: *Refund* (part or all of it).

Other paths: *Reject* in the restaurant panel; *Card declines*; *Cancel order*
before pickup; *Decline* or *Drop out* as rider 1 (rider 2 gets it); or don't
answer — the restaurant times out after 5 minutes, a rider offer after 60s.
*Mark ready* can come before a rider is found. Left alone, a kitchen that never
marks the food ready is flagged 15 minutes after it was due and the order
cancelled after an hour; a rider who doesn't come for ready food is replaced
15 minutes after their pickup ETA. **Admin** → *Mark delivered* settles a
picked-up order the rider never marked delivered.

The **workflow** link next to an order opens it in the Temporal UI, showing
every saga step, signal and compensation.

## Files

| File | What |
|---|---|
| `index.html`, `styles.css` | Layout |
| `js/config.js` | Gateway URL, demo accounts |
| `js/accounts.js` | Logins and tokens |
| `js/api.js` | `fetch` through the gateway (re-login on 401) |
| `js/sse.js` | Live feeds (`EventSource`, token in `?access_token=`) |
| `js/customer.js`, `restaurant.js`, `rider.js`, `admin.js` | One panel each |
| `js/main.js` | Wires the panels together |
