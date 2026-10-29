import { notifications } from "@mantine/notifications";
import { useCallback, useEffect, useRef, useState } from "react";
import { getSession, get, refreshAccess, type Page } from "./api";
import { GATEWAY } from "./config";

export const showError = (error: unknown) =>
  notifications.show({ color: "red", title: "Failed", message: (error as Error).message });

// Run a button's action: a toast with the API's message on failure, an
// optional one on success. Returns the result, or undefined if it failed.
export async function run<T>(action: () => Promise<T>, success?: string): Promise<T | undefined> {
  try {
    const result = await action();
    if (success) notifications.show({ color: "green", message: success });
    return result;
  } catch (error) {
    showError(error);
    return undefined;
  }
}

// Load data on mount (and when `deps` change); `reload` re-runs it.
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | undefined>();
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await loadRef.current());
    } catch (error) {
      showError(error);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => void reload(), deps);
  return { data, setData, loading, reload };
}

// A cursor-paginated list: the first page on load, `more` appends the next.
export function usePaged<T>(path: string | null) {
  const [items, setItems] = useState<T[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const fetchPage = useCallback(
    async (after: string | null) => {
      if (!path) return;
      setLoading(true);
      try {
        const sep = path.includes("?") ? "&" : "?";
        const page = await get<Page<T>>(after ? `${path}${sep}cursor=${encodeURIComponent(after)}` : path);
        setItems((prev) => (after ? [...prev, ...page.items] : page.items));
        setCursor(page.next_cursor ?? null);
      } catch (error) {
        showError(error);
      } finally {
        setLoading(false);
      }
    },
    [path],
  );
  useEffect(() => void fetchPage(null), [fetchPage]);
  return {
    items,
    loading,
    hasMore: cursor !== null,
    more: () => fetchPage(cursor),
    reload: () => fetchPage(null),
  };
}

export type FeedState = "connecting" | "open" | "closed";
type Handlers = Record<string, (data: any) => void>;

// A live feed (tracking-service via the gateway). EventSource can't send
// headers, so the token goes in ?access_token=. If the server refuses or
// drops the feed for good (e.g. the token expired), refresh the token and
// reconnect, up to 3 times in a row.
export function useFeed(path: string | null, handlers: Handlers): FeedState {
  const [state, setState] = useState<FeedState>("connecting");
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    if (!path) return;
    let source: EventSource | null = null;
    let failures = 0;
    let stopped = false;
    let retry: ReturnType<typeof setTimeout>;

    const open = () => {
      setState("connecting");
      const token = encodeURIComponent(getSession()?.access_token ?? "");
      source = new EventSource(`${GATEWAY}${path}?access_token=${token}`);
      source.onopen = () => {
        failures = 0;
        setState("open");
      };
      for (const name of Object.keys(handlersRef.current)) {
        source.addEventListener(name, (e) => handlersRef.current[name]?.(JSON.parse((e as MessageEvent).data)));
      }
      source.onerror = () => {
        if (source?.readyState !== EventSource.CLOSED || stopped) return;
        setState("closed");
        if (++failures > 3) return;
        retry = setTimeout(async () => {
          if (!stopped && (await refreshAccess())) open();
        }, 2000);
      };
    };
    open();
    return () => {
      stopped = true;
      clearTimeout(retry);
      source?.close();
    };
  }, [path]);

  return state;
}

// Re-render every `ms` (offer countdowns).
export function useTick(ms: number) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), ms);
    return () => clearInterval(id);
  }, [ms]);
}
