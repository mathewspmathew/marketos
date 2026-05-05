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

    // Following the pattern from app._index.jsx for the demo user
    const demoUserId = process.env.MARKETOS_DEMO_TENANT_ID || "00000000-0000-0000-0000-000000000001";
    
    console.log(`[Controller Loader] Fetching configs for shop: ${shop}, user: ${demoUserId}`);
    
    const configs = await db.scrapingConfig.findMany({
      where: {
        shopDomain: shop,
      },
      orderBy: { createdAt: "desc" },
    });

    console.log(`[Controller Loader] Found ${configs.length} configs`);

    return { configs, shop, demoUserId };
  } catch (error) {
    console.error("[Controller Loader] Error:", error);
    throw error;
  }
};

// ─── Action ──────────────────────────────────────────────────────────────────
export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const formData = await request.formData();
  const intent = formData.get("intent");

  const demoUserId = process.env.MARKETOS_DEMO_TENANT_ID || "00000000-0000-0000-0000-000000000001";

  if (intent === "addConfig") {
    const competitorUrl = formData.get("competitorUrl");
    const includeImages = formData.get("includeImages") === "true";

    let productLimitRaw = formData.get("productLimit");
    if (productLimitRaw === "custom") {
      productLimitRaw = formData.get("customProductLimit");
    }
    const productLimit = productLimitRaw ? parseInt(productLimitRaw, 10) : null;
    
    const frequencyUnit = formData.get("frequencyUnit") || "nofreq";
    const frequencyIntervalRaw = formData.get("frequencyInterval");
    const frequencyInterval = frequencyUnit !== "nofreq"
      ? (parseInt(frequencyIntervalRaw, 10) || 1)
      : null;

    await db.scrapingConfig.create({
      data: {
        shopDomain: shop,
        competitorUrl,
        includeImages,
        productLimit: isNaN(productLimit) ? null : productLimit,
        frequencyInterval,
        frequencyUnit,
      },
    });
  } else if (intent === "deleteConfig") {
    const configId = formData.get("configId");
    await db.scrapingConfig.delete({
      where: { id: configId },
    });
  }

  return { success: true };
};

// ─── UI ───────────────────────────────────────────────────────────────────────
export default function ControllerPage() {
  const { configs } = useLoaderData();
  const fetcher = useFetcher();

  const [competitorUrl, setCompetitorUrl] = useState("");
  const [includeImages, setIncludeImages] = useState(true);
  
  const [productLimit, setProductLimit] = useState("10");
  const [customProductLimit, setCustomProductLimit] = useState("");
  
  const [frequencyUnit, setFrequencyUnit] = useState("nofreq");
  const [frequencyInterval, setFrequencyInterval] = useState("1");

  const isAdding = fetcher.state === "submitting" && fetcher.formData?.get("intent") === "addConfig";

  const handleAdd = () => {
    if (!competitorUrl) return;
    
    fetcher.submit(
      {
        intent: "addConfig",
        competitorUrl,
        includeImages: String(includeImages),
        productLimit,
        customProductLimit,
        frequencyInterval,
        frequencyUnit,
      },
      { method: "POST" }
    );
    
    setCompetitorUrl("");
  };

  const handleDelete = (id) => {
    if (confirm("Are you sure you want to remove this competitor?")) {
      fetcher.submit(
        { intent: "deleteConfig", configId: id },
        { method: "POST" }
      );
    }
  };

  return (
    <s-page heading="Scraping Controller">
      <s-stack direction="block" gap="loose">
        
        {/* ── Add New Competitor Section ── */}
        <s-section heading="Add Competitor Website">
          <s-stack direction="block" gap="base">
            <s-text-field
              label="Competitor URL"
              placeholder="https://competitor.com"
              value={competitorUrl}
              onInput={(e) => setCompetitorUrl(e.currentTarget.value)}
              helpText="Enter the full URL of the competitor's website or product page."
            />
            
            <s-stack direction="inline" gap="base" align="center">
               <s-text>Include Images</s-text>
               <s-toggle 
                  id="include-images" 
                  checked={includeImages || undefined} 
                  onClick={() => setIncludeImages(!includeImages)} 
                />
            </s-stack>

            <s-stack direction="block" gap="tight">
              <s-text emphasis="bold">Products Limit</s-text>
              <s-stack direction="inline" gap="base">
                {["10", "20", "30", "50", "custom"].map((val) => (
                  <s-button 
                    key={val}
                    variant={productLimit === val ? "primary" : "secondary"}
                    onClick={() => setProductLimit(val)}
                  >
                    {val === "custom" ? "Custom" : val}
                  </s-button>
                ))}
              </s-stack>
              {productLimit === "custom" && (
                <s-text-field
                  type="number"
                  placeholder="Enter limit"
                  value={customProductLimit}
                  onInput={(e) => setCustomProductLimit(e.currentTarget.value)}
                />
              )}
            </s-stack>

            <s-stack direction="block" gap="tight">
              <s-text emphasis="bold">Re-scrape Frequency</s-text>
              <s-stack direction="inline" gap="base">
                {[
                  { val: "nofreq", label: "No Freq" },
                  { val: "min",    label: "Min" },
                  { val: "hr",     label: "Hr" },
                  { val: "day",    label: "Day" },
                ].map(({ val, label }) => (
                  <s-button
                    key={val}
                    variant={frequencyUnit === val ? "primary" : "secondary"}
                    onClick={() => setFrequencyUnit(val)}
                  >
                    {label}
                  </s-button>
                ))}
              </s-stack>
              {frequencyUnit !== "nofreq" && (
                <s-text-field
                  type="number"
                  label={`Every how many ${frequencyUnit === "min" ? "minutes" : frequencyUnit === "hr" ? "hours" : "days"}?`}
                  placeholder="1"
                  value={frequencyInterval}
                  onInput={(e) => setFrequencyInterval(e.currentTarget.value)}
                />
              )}
            </s-stack>

            <s-button 
              variant="primary" 
              onClick={handleAdd}
              disabled={!competitorUrl || isAdding}
            >
              {isAdding ? "Adding..." : "Add Competitor"}
            </s-button>
          </s-stack>
        </s-section>

        {/* ── Configured Competitors List ── */}
        <s-section heading="Managed Competitors">
          {configs.length === 0 ? (
            <s-paragraph tone="subdued">No competitors configured yet.</s-paragraph>
          ) : (
            <s-resource-list>
              {configs.map((config) => (
                <s-resource-item key={config.id} id={config.id}>
                  <s-stack direction="block" gap="tight">
                    <s-stack direction="inline" gap="base" align="center">
                      <s-text emphasis="bold">{config.competitorUrl}</s-text>
                      <s-badge tone={config.isActive ? "success" : "subdued"}>
                        {config.isActive ? "Active" : "Inactive"}
                      </s-badge>
                    </s-stack>
                    
                    <s-stack direction="inline" gap="loose">
                      <s-text tone="subdued">Limit: {config.productLimit ?? "None"}</s-text>
                      <s-text tone="subdued">
                        Freq: {config.frequencyUnit === "nofreq" || !config.frequencyUnit
                          ? "None"
                          : `Every ${config.frequencyInterval} ${config.frequencyUnit}`}
                      </s-text>
                      <s-text tone="subdued">Images: {config.includeImages ? "Yes" : "No"}</s-text>
                    </s-stack>

                    <s-stack direction="inline" gap="base">
                      <s-button 
                        variant="plain" 
                        tone="critical" 
                        onClick={() => handleDelete(config.id)}
                      >
                        Remove
                      </s-button>
                    </s-stack>
                  </s-stack>
                </s-resource-item>
              ))}
            </s-resource-list>
          )}
        </s-section>

      </s-stack>
    </s-page>
  );
}

export function ErrorBoundary() {
  const error = useRouteError();
  console.error("[Controller ErrorBoundary]", error);
  return boundary.error(error);
}

export const headers = (headersArgs) => {
  return boundary.headers(headersArgs);
}
