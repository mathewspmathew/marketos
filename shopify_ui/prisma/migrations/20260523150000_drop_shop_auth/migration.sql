-- ShopAuth was a planned offline-token table that we never wired up; the
-- Session table (written by shopify-app-react-router) already stores tokens.
DROP TABLE IF EXISTS "ShopAuth";
