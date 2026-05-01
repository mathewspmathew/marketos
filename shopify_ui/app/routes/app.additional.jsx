import { useState } from "react";
import { useFetcher, useLoaderData, useRouteError } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

// ─── Loader ──────────────────────────────────────────────────────────────────
export const loader = async ({ request }) => {
  try {
    const { session } = await authenticate.admin(request);
    const shop = session.shop;

    // Consistency with other pages: pull the demo user id
    const demoUserId = process.env.MARKETOS_DEMO_TENANT_ID || "00000000-0000-0000-0000-000000000001";
    
    // Fetch configs filtered by shop and current user
    const configs = await db.scrapingConfig.findMany({
      where: { 
        userId: demoUserId,
        shopId: shop
      },
      orderBy: { createdAt: "desc" },
    });

    return { configs, shop, demoUserId };
  } catch (error) {
    console.error("[Scraper Loader] Error:", error);
    throw error;
  }
};

// ─── Action ──────────────────────────────────────────────────────────────────
export const action = async ({ request }) => {
  try {
    const { session } = await authenticate.admin(request);
    const shop = session.shop;
    const formData = await request.formData();
    const intent = formData.get("intent");

    const demoUserId = process.env.MARKETOS_DEMO_TENANT_ID || "00000000-0000-0000-0000-000000000001";

    if (intent === "createScrape") {
      const competitorUrl = formData.get("competitorUrl");
      const productLimit = parseInt(formData.get("productLimit") || "5", 10);

      await db.scrapingConfig.create({
        data: {
          userId: demoUserId,
          shopId: shop,
          competitorUrl,
          productLimit,
          status: "IDLE", // Defaults to IDLE for Celery Beat pick-up
          isActive: true,
        },
      });
    }

    return { success: true };
  } catch (error) {
    console.error("[Scraper Action] Error:", error);
    return { success: false, error: error.message };
  }
};

// ─── UI ───────────────────────────────────────────────────────────────────────
export default function AdditionalPage() {
  const { configs } = useLoaderData();
  const fetcher = useFetcher();

  const [url, setUrl] = useState("");
  const [limit, setLimit] = useState("5");

  const isSubmitting = fetcher.state === "submitting" && fetcher.formData?.get("intent") === "createScrape";

  const handleStartScrape = () => {
    if (!url) return;
    
    fetcher.submit(
      {
        intent: "createScrape",
        competitorUrl: url,
        productLimit: limit,
      },
      { method: "POST" }
    );
    
    // Clear the URL field after submission
    setUrl("");
  };

  return (
    <s-page heading="Firecrawl Microservice Scraper">
      <s-stack direction="block" gap="loose">
        
        {/* ── Configuration Section ── */}
        <s-section heading="Launch New Scrape">
          <s-stack direction="block" gap="base">
            <s-paragraph>
              Enter a competitor's collection or search listing URL. 
              The <strong>Celery Beat</strong> scheduler will detect new tasks and trigger 
              the parallel scraping workers automatically.
            </s-paragraph>
            
            <s-card>
              <s-box padding="extra-loose">
                <s-stack direction="block" gap="base">
                  <s-text-field
                    label="Competitor Listing URL"
                    placeholder="https://www.competitor.com/collections/all"
                    value={url}
                    onInput={(e) => setUrl(e.currentTarget.value)}
                    helpText="Supports Flipkart, Amazon, Myntra, and more via Firecrawl."
                  />
                  
                  <s-select
                    label="Product Sample Size"
                    value={limit}
                    onChange={(e) => setLimit(e.currentTarget.value)}
                  >
                    <s-option value="5">5 Products</s-option>
                    <s-option value="10">10 Products</s-option>
                    <s-option value="25">25 Products</s-option>
                    <s-option value="50">50 Products</s-option>
                  </s-select>

                  <s-button 
                    variant="primary" 
                    onClick={handleStartScrape}
                    disabled={!url || isSubmitting}
                  >
                    {isSubmitting ? "Sending to Queue..." : "Start Scraper"}
                  </s-button>
                </s-stack>
              </s-box>
            </s-card>
          </s-stack>
        </s-section>

        {/* ── Active Tasks & History ── */}
        <s-section heading="Real-time Task Status">
          {configs.length === 0 ? (
            <s-paragraph tone="subdued">No scrapes have been initiated yet.</s-paragraph>
          ) : (
            <s-resource-list>
              {configs.map((config) => {
                // Determine badge tone based on status enum
                let tone = "subdued";
                if (config.status === "RUNNING") tone = "info";
                if (config.status === "QUEUED") tone = "attention";
                if (config.status === "SCRAPED_FIRST") tone = "success";

                return (
                  <s-resource-item key={config.id} id={config.id}>
                    <s-stack direction="block" gap="tight">
                      <s-stack direction="inline" gap="base" align="center">
                        <s-text emphasis="bold">{config.competitorUrl}</s-text>
                        <s-badge tone={tone}>{config.status}</s-badge>
                      </s-stack>
                      
                      <s-stack direction="inline" gap="loose">
                        <s-text tone="subdued">Limit: {config.productLimit}</s-text>
                        <s-text tone="subdued">
                          Requested: {new Date(config.createdAt).toLocaleTimeString()}
                        </s-text>
                        {config.status === "SCRAPED_FIRST" && (
                          <s-text tone="success">✓ Extraction Complete</s-text>
                        )}
                      </s-stack>
                    </s-stack>
                  </s-resource-item>
                );
              })}
            </s-resource-list>
          )}
        </s-section>

      </s-stack>
    </s-page>
  );
}

export function ErrorBoundary() {
  const error = useRouteError();
  return boundary.error(error);
}

export const headers = (headersArgs) => {
  return boundary.headers(headersArgs);
}
