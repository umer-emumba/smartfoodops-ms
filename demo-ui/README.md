# Demo UIs

Two independent tools for testing SmartFoodOps by hand. Local only — both
log in with the seeded demo accounts. They share no code.

| Directory | What | Port |
|---|---|---|
| [static/](static/README.md) | Plain HTML + JS: every role side by side on one page, no build step | 3000 |
| [react/](react/README.md) | React app: a portal per role (customer, restaurant, rider, admin) | 5173 |

They can run at the same time. The static page calls the gateway directly
(`CORS_ORIGINS=http://localhost:3000` in `services/api-gateway/.env`); the
React dev server proxies to it, so it needs no CORS entry.
