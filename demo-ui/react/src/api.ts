// fetch() through the gateway. Tokens live in sessionStorage, so each browser
// tab keeps its own login (customer in one tab, restaurant in another). On a
// 401 the access token is refreshed once and the call retried.
import { GATEWAY } from "./config";

export type Role = "customer" | "restaurant_admin" | "rider" | "system_admin";

// Where each role lands after logging in.
export const HOME: Record<Role, string> = {
  customer: "/customer",
  restaurant_admin: "/restaurant",
  rider: "/rider",
  system_admin: "/admin",
};

export interface User {
  id: string;
  email: string;
  full_name: string;
  phone_number: string;
  role: Role;
  is_blocked: boolean;
  created_at: string;
}

interface Session {
  access_token: string;
  refresh_token: string;
  user: User;
}

const KEY = "sfo-session";

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export function getSession(): Session | null {
  const raw = sessionStorage.getItem(KEY);
  return raw ? (JSON.parse(raw) as Session) : null;
}

function setSession(session: Session | null) {
  if (session) sessionStorage.setItem(KEY, JSON.stringify(session));
  else sessionStorage.removeItem(KEY);
}

async function parse<T>(resp: Response): Promise<T> {
  const data = await resp.json().catch(() => null);
  if (!resp.ok) throw new ApiError(data?.error?.message ?? `HTTP ${resp.status}`, resp.status);
  return (data ?? {}) as T; // {} for 204s, so success is always truthy
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(GATEWAY + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<T>(resp);
}

export async function login(email: string, password: string): Promise<User> {
  const tokens = await post<{ access_token: string; refresh_token: string }>("/v1/auth/login", {
    email,
    password,
  });
  const resp = await fetch(`${GATEWAY}/v1/users/me`, {
    headers: { Authorization: `Bearer ${tokens.access_token}` },
  });
  const user = await parse<User>(resp);
  setSession({ ...tokens, user });
  return user;
}

export async function register(body: {
  email: string;
  password: string;
  full_name: string;
  phone_number: string;
  role: Role;
}): Promise<User> {
  await post("/v1/auth/register", body);
  return login(body.email, body.password);
}

export async function logout() {
  const session = getSession();
  setSession(null);
  if (session) await post("/v1/auth/logout", { refresh_token: session.refresh_token }).catch(() => {});
}

// One refresh at a time, shared by every call that hit a 401 together.
let refreshing: Promise<boolean> | null = null;

export function refreshAccess(): Promise<boolean> {
  refreshing ??= (async () => {
    const session = getSession();
    if (!session) return false;
    try {
      const tokens = await post<{ access_token: string; refresh_token: string }>("/v1/auth/refresh", {
        refresh_token: session.refresh_token,
      });
      setSession({ ...session, ...tokens });
      return true;
    } catch {
      setSession(null);
      window.location.assign("/login");
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

function send(method: string, path: string, body?: unknown, headers: Record<string, string> = {}) {
  const all: Record<string, string> = { Authorization: `Bearer ${getSession()?.access_token}`, ...headers };
  if (body !== undefined) all["Content-Type"] = "application/json";
  return fetch(GATEWAY + path, {
    method,
    headers: all,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export async function api<T = unknown>(
  method: string,
  path: string,
  body?: unknown,
  headers?: Record<string, string>,
): Promise<T> {
  let resp = await send(method, path, body, headers);
  if (resp.status === 401 && (await refreshAccess())) resp = await send(method, path, body, headers);
  return parse<T>(resp);
}

export const get = <T>(path: string) => api<T>("GET", path);

// GET that returns null when the thing isn't there (yet) or isn't ours to see,
// e.g. a payment before authorization, or a delivery the restaurant can't read.
export async function find<T>(path: string): Promise<T | null> {
  try {
    return await get<T>(path);
  } catch (error) {
    if (error instanceof ApiError && [403, 404].includes(error.status)) return null;
    throw error;
  }
}

export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
}
