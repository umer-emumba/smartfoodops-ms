// Access tokens for the demo accounts, by name ("customer", "rider1", ...).
import { ACCOUNTS, GATEWAY, PASSWORD } from "./config.js";

const tokens = {};

export const token = (who) => tokens[who];

export async function login(who) {
  const resp = await fetch(`${GATEWAY}/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: ACCOUNTS[who], password: PASSWORD }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(`${who} login failed: ${data.error?.message ?? resp.status} — run scripts/seed.py?`);
  }
  tokens[who] = data.access_token;
}

export async function loginAll() {
  for (const who of Object.keys(ACCOUNTS)) await login(who);
}
