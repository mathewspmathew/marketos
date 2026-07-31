// routes.js — enables React Router's flat-routes convention: every file in
// routes/ is auto-discovered as a route by filename, no manual route table.
import { flatRoutes } from "@react-router/fs-routes";

export default flatRoutes({
  ignoredRouteFiles: ["**/*.test.{js,jsx,ts,tsx}"],
});
