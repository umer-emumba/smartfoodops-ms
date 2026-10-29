// fetch() through the gateway as one of the demo accounts. Logs in again
// once on 401 (access tokens last 15 minutes). Throws Error(message) on
// any non-2xx, with the API's error message.
import { login, token } from "./accounts.js";
import { GATEWAY } from "./config.js";

function send(who, method, path, body, headers) {
  const all = { Authorization: `Bearer ${token(who)}`, ...headers };
  if (body !== undefined) all["Content-Type"] = "application/json";
  return fetch(GATEWAY + path, {
    method,
    headers: all,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export async function api(who, method, path, body, headers = {}) {
  let resp = await send(who, method, path, body, headers);
  if (resp.status === 401) {
    await login(who);
    resp = await send(who, method, path, body, headers);
  }
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const error = new Error(data?.error?.message ?? `HTTP ${resp.status}`);
    error.status = resp.status;
    throw error;
  }
  return data;
}

// GET that returns null on 404 ("not there yet"), e.g. a payment before authorization.
export async function find(who, path) {
  try {
    return await api(who, "GET", path);
  } catch (error) {
    if (error.status === 404) return null;
    throw error;
  }
}
