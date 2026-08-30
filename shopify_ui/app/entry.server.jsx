/**
 * entry.server.jsx — React Router SSR entry point. Streams the rendered
 * HTML response, waiting for the full page (onAllReady) for bots/crawlers
 * so they get complete markup, or streaming as soon as the shell is ready
 * (onShellReady) for real users. Framework boilerplate, not app logic.
 */
import { PassThrough } from "stream";
import { renderToPipeableStream } from "react-dom/server";
import { ServerRouter } from "react-router";
import { createReadableStreamFromReadable } from "@react-router/node";
import { isbot } from "isbot";
import * as Sentry from "@sentry/node";
import { addDocumentResponseHeaders } from "./shopify.server";

// No-op when unset (local dev) — same gate as the Python side's
// services/common/logging_config.py.
if (process.env.SENTRY_DSN) {
  Sentry.init({
    dsn: process.env.SENTRY_DSN,
    environment: process.env.SENTRY_ENVIRONMENT || "production",
  });
}

export const streamTimeout = 5000;

// React Router's app-wide server-error hook — called for every loader/
// action/render error. Exporting this disables the framework's own default
// console logging, so console.error stays in explicitly for parity with
// today's dozzle/Cloud Logging visibility, plus Sentry now.
export function handleError(error, { request }) {
  if (!request.signal.aborted) {
    Sentry.captureException(error);
    console.error(error);
  }
}

export default async function handleRequest(
  request,
  responseStatusCode,
  responseHeaders,
  reactRouterContext,
) {
  addDocumentResponseHeaders(request, responseHeaders);
  const userAgent = request.headers.get("user-agent");
  const callbackName = isbot(userAgent ?? "") ? "onAllReady" : "onShellReady";

  return new Promise((resolve, reject) => {
    const { pipe, abort } = renderToPipeableStream(
      <ServerRouter context={reactRouterContext} url={request.url} />,
      {
        [callbackName]: () => {
          const body = new PassThrough();
          const stream = createReadableStreamFromReadable(body);

          responseHeaders.set("Content-Type", "text/html");
          resolve(
            new Response(stream, {
              headers: responseHeaders,
              status: responseStatusCode,
            }),
          );
          pipe(body);
        },
        onShellError(error) {
          Sentry.captureException(error);
          reject(error);
        },
        onError(error) {
          responseStatusCode = 500;
          Sentry.captureException(error);
          console.error(error);
        },
      },
    );

    // Automatically timeout the React renderer after 6 seconds, which ensures
    // React has enough time to flush down the rejected boundary contents
    setTimeout(abort, streamTimeout + 1000);
  });
}
