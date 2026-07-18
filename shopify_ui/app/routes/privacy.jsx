const EFFECTIVE_DATE = "July 13, 2026";
const CONTACT_EMAIL = "mathewsmathewsp@gmail.com";

export default function PrivacyPolicy() {
  return (
    <div style={{ maxWidth: "720px", margin: "0 auto", padding: "48px 24px", lineHeight: 1.6, fontFamily: "system-ui, sans-serif" }}>
      <h1>Privacy Policy</h1>
      <p>Effective date: {EFFECTIVE_DATE}</p>

      <p>
        MarketOS (&quot;we&quot;, &quot;our&quot;, &quot;the app&quot;) is a Shopify-embedded app that helps
        merchants track competitor pricing and manage dynamic pricing for
        their own store. This policy explains what data we collect through
        the app, how we use it, and how it is retained and deleted.
      </p>

      <h2>Information We Collect</h2>
      <p>When a merchant installs MarketOS, we collect and store:</p>
      <ul>
        <li>
          <strong>Shop and account data:</strong> your shop domain, Shopify
          account email and name, and OAuth access tokens needed to act on
          your store.
        </li>
        <li>
          <strong>Store data:</strong> product, variant, price, and inventory
          information synced from your Shopify store, used to power pricing
          and matching.
        </li>
        <li>
          <strong>Competitor/market data:</strong> publicly available product
          and pricing information we collect from third-party websites you
          configure, for comparison against your store.
        </li>
        <li>
          <strong>App usage data:</strong> pricing rules, settings, and
          chat/assistant conversations you have within the app, used to
          operate and improve the features you use.
        </li>
      </ul>
      <p>
        <strong>We do not collect data about your store&apos;s customers or
        shoppers.</strong> MarketOS does not access, store, or process any
        personal information belonging to the people who shop at your store.
      </p>

      <h2>How We Use Information</h2>
      <p>We use the data described above to:</p>
      <ul>
        <li>Operate core app features: competitor discovery, price matching, and dynamic pricing suggestions/automation.</li>
        <li>Sync your product catalog and apply price changes you&apos;ve configured.</li>
        <li>Provide the in-app chat assistant and query tools.</li>
        <li>Diagnose issues and maintain the reliability of the app.</li>
      </ul>
      <p>We do not sell your data, and we do not use it for advertising.</p>

      <h2>Third-Party Service Providers</h2>
      <p>
        To provide the app&apos;s functionality, we share the minimum necessary
        data with the following service providers, each processing data on
        our behalf under their own terms:
      </p>
      <ul>
        <li><strong>Shopify</strong> — the platform the app is built on.</li>
        <li><strong>Firecrawl</strong> — fetches publicly available competitor web pages for scraping.</li>
        <li><strong>Groq</strong> — powers LLM-based text extraction and the in-app chat assistant.</li>
        <li><strong>Google Cloud (Vertex AI, Cloud Storage)</strong> — generates product embeddings and stores scraped product images/content.</li>
        <li><strong>Serper</strong> — powers competitor product discovery search.</li>
      </ul>

      <h2>Data Retention and Deletion</h2>
      <p>
        We retain your shop&apos;s data for as long as the app remains installed
        on your store. When you uninstall MarketOS, Shopify notifies us
        automatically, and we permanently delete all data associated with
        your shop — including products, pricing history, competitor matches,
        settings, and chat history — within 48 hours.
      </p>
      <p>
        MarketOS implements Shopify&apos;s mandatory compliance webhooks
        (<code>customers/data_request</code>, <code>customers/redact</code>,
        and <code>shop/redact</code>) so that data requests and deletions are
        honored automatically and promptly.
      </p>

      <h2>Your Rights</h2>
      <p>
        Depending on your location, you may have rights under data protection
        laws (such as GDPR or CCPA) to access, correct, or request deletion of
        your data. Since MarketOS only stores shop-level and product data —
        not personal data about your store&apos;s customers — these requests apply
        to your own merchant account data. To exercise these rights, contact
        us at <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
      </p>

      <h2>Security</h2>
      <p>
        We use industry-standard measures to protect your data, including
        encrypted connections (HTTPS) for all data in transit and access
        controls restricting who can view stored data. No method of
        transmission or storage is 100% secure, but we work to protect your
        information using commercially reasonable safeguards.
      </p>

      <h2>Children&apos;s Privacy</h2>
      <p>
        MarketOS is a business tool intended for use by merchants and is not
        directed at, nor knowingly used to collect data from, children.
      </p>

      <h2>Changes to This Policy</h2>
      <p>
        We may update this policy from time to time. Changes will be posted
        on this page with an updated effective date.
      </p>

      <h2>Contact Us</h2>
      <p>
        Questions about this policy or your data can be sent to{" "}
        <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
      </p>
    </div>
  );
}
