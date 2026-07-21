// auth.$.jsx — OAuth catch-all route. authenticate.admin() handles the
// entire install/re-auth redirect dance; there's nothing else to render.
import { boundary } from "@shopify/shopify-app-react-router/server";
import { authenticate } from "../shopify.server";

export const loader = async ({ request }) => {
  await authenticate.admin(request);

  return null;
};

export const headers = (headersArgs) => {
  return boundary.headers(headersArgs);
};
