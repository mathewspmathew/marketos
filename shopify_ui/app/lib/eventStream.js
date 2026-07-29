/**
 * eventStream.js — manual SSE consumer using fetch() instead of native
 * EventSource. This Shopify embedded app authenticates via App Bridge,
 * which patches the global `fetch` to attach the session-token
 * Authorization header every request needs (see shopify_ui/app/routes/
 * app.jsx's <AppProvider embedded>). Native EventSource never goes through
 * `fetch`, so it never carries that token — every EventSource connection to
 * a Shopify-session-authenticated route gets redirected to /login and
 * silently retry-loops forever. Reading the stream by hand through fetch()
 * gets the token automatically and actually authenticates.
 */

const RECONNECT_INITIAL_MS = 1000;
const RECONNECT_MAX_MS = 30_000;

// Splits one decoded SSE "event: X\ndata: Y\n\n" block into {event, data}.
function parseEventBlock(rawEvent) {
  let event = "message";
  let data = "";
  for (const line of rawEvent.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  return { event, data };
}

/**
 * Subscribes to an SSE endpoint via fetch(), invoking onMessage(data) for
 * every event whose name matches eventName. Reconnects with capped
 * exponential backoff if the stream drops (mirrors the reconnect-with-backoff
 * used server-side in services/api_gateway/live_updates.py). Returns a
 * cleanup function that aborts the connection and stops reconnecting.
 */
export function subscribeToEventStream(url, eventName, onMessage) {
  const controller = new AbortController();
  let backoff = RECONNECT_INITIAL_MS;

  async function connectOnce() {
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok || !res.body) {
      throw new Error(`event stream fetch failed: ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    backoff = RECONNECT_INITIAL_MS; // connected cleanly — reset backoff

    let done = false;
    while (!done) {
      const chunk = await reader.read();
      done = chunk.done;
      if (done) break;
      buffer += decoder.decode(chunk.value, { stream: true });

      let sepIndex;
      while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        const { event, data } = parseEventBlock(rawEvent);
        if (event === eventName) onMessage(data);
      }
    }
  }

  (async () => {
    while (!controller.signal.aborted) {
      try {
        await connectOnce();
      } catch (err) {
        if (controller.signal.aborted || err.name === "AbortError") return;
      }
      if (controller.signal.aborted) return;
      await new Promise((resolve) => setTimeout(resolve, backoff));
      backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
    }
  })();

  return () => controller.abort();
}
