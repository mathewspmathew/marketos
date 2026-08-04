/**
 * _index/route.jsx — unauthenticated landing page, shown only when there's
 * no ?shop= param (otherwise redirects straight to /app). No shop-domain
 * entry form here — App Store rule 2.3.1 requires installs to start only
 * from a Shopify-owned surface, not a manually-typed domain on this page.
 * NOTE: still has unedited Shopify CLI scaffold placeholder copy
 * ("[your app]", generic "Product feature" bullets) — needs real marketing
 * copy before this is shown to anyone for real, or reconsider whether it's
 * needed at all.
 */
import { redirect } from "react-router";
import styles from "./styles.module.css";

export const loader = async ({ request }) => {
  const url = new URL(request.url);

  if (url.searchParams.get("shop")) {
    throw redirect(`/app?${url.searchParams.toString()}`);
  }

  return null;
};

export default function App() {
  return (
    <div className={styles.index}>
      <div className={styles.content}>
        <h1 className={styles.heading}>A short heading about [your app]</h1>
        <p className={styles.text}>
          A tagline about [your app] that describes your value proposition.
        </p>
        <ul className={styles.list}>
          <li>
            <strong>Product feature</strong>. Some detail about your feature and
            its benefit to your customer.
          </li>
          <li>
            <strong>Product feature</strong>. Some detail about your feature and
            its benefit to your customer.
          </li>
          <li>
            <strong>Product feature</strong>. Some detail about your feature and
            its benefit to your customer.
          </li>
        </ul>
      </div>
    </div>
  );
}
