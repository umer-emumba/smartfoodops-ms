// Live feeds (tracking-service via the gateway). EventSource can't send
// headers, so the token goes in ?access_token= (the gateway moves it into
// the Authorization header).
import { token } from "./accounts.js";
import { GATEWAY } from "./config.js";

// `handlers` maps event name -> fn(data). Returns a function that closes
// the feed. `onClosed` runs if the server refuses or drops it for good.
export function openFeed(who, path, handlers, onClosed) {
  const url = `${GATEWAY}${path}?access_token=${encodeURIComponent(token(who))}`;
  const source = new EventSource(url);
  for (const [name, handle] of Object.entries(handlers)) {
    source.addEventListener(name, (e) => handle(JSON.parse(e.data)));
  }
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) onClosed?.();
  };
  return () => source.close();
}
