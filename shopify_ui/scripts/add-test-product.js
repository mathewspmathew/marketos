/**
 * scripts/add-test-product.js — add ONE scraped product to the dev store
 *
 * Writes ONLY to the Shopify store (productCreate + variant price). It does NOT
 * call the api_gateway, pull_products, or any pipeline trigger. The product is
 * expected to flow into Postgres automatically via the products/create webhook
 * → semantic → embedding pipeline. This script exists to test that automatic path.
 *
 *   node --env-file=.env scripts/add-test-product.js [--shop=your-store.myshopify.com]
 *
 * Not idempotent — re-running creates duplicates.
 */
import "@shopify/shopify-app-react-router/adapters/node";
import {
  ApiVersion,
  AppDistribution,
  shopifyApp,
} from "@shopify/shopify-app-react-router/server";
import { PrismaSessionStorage } from "@shopify/shopify-app-session-storage-prisma";
import { PrismaClient } from "@prisma/client";

const prisma = new PrismaClient();

const shopify = shopifyApp({
  apiKey: process.env.SHOPIFY_API_KEY,
  apiSecretKey: process.env.SHOPIFY_API_SECRET || "",
  apiVersion: ApiVersion.October25,
  scopes: process.env.SCOPES?.split(","),
  appUrl: process.env.SHOPIFY_APP_URL || process.env.WEBHOOK_BASE_URL || "https://localhost",
  authPathPrefix: "/auth",
  sessionStorage: new PrismaSessionStorage(prisma),
  distribution: AppDistribution.AppStore,
  future: { expiringOfflineAccessTokens: true },
});

const PRODUCT = {
  title: "Casio FX-82MS 2nd Gen Non-Programmable Scientific Calculator - 240 Functions, 2-line Display",
  vendor: "Casio",
  productType: "Calculators",
  tags: ["stationery", "school", "exam", "calculator", "scientific"],
  descriptionHtml:
    "<p>2nd edition scientific calculator with a sleek, compact curved-edge design. 240 functions including trigonometric, logarithmic, permutation and combination. Colour-coded keypad for easy key differentiation, drop-resistant body with water-resistant key printing, 2-line display. Runs on one AAA battery and comes with a slide-on hard case.</p><ul><li>240 functions, 2-line display</li><li>Non-programmable - exam friendly</li><li>Slide-on hard case included</li></ul>",
  image: "https://m.media-amazon.com/images/I/61%2B56mXx2PL._SL1000_.jpg",
  price: "595.00",
  compareAtPrice: "650.00",
  sku: "CASIO-FX82MS",
};

const PRODUCT_CREATE = `
  mutation addProduct($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
    productCreate(product: $product, media: $media) {
      product { id title variants(first: 1) { nodes { id } } }
      userErrors { field message }
    }
  }
`;

const VARIANTS_BULK_UPDATE = `
  mutation addVariant($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
    productVariantsBulkUpdate(productId: $productId, variants: $variants) {
      productVariants { id price compareAtPrice sku }
      userErrors { field message }
    }
  }
`;

async function gql(admin, query, variables) {
  const resp = await admin.graphql(query, { variables });
  const body = await resp.json();
  if (body.errors) throw new Error(`GraphQL transport error: ${JSON.stringify(body.errors)}`);
  return body.data;
}

async function resolveShop() {
  const flag = process.argv.find((a) => a.startsWith("--shop="));
  if (flag) return flag.split("=")[1];
  if (process.env.SHOP) return process.env.SHOP;
  const session = await prisma.session.findFirst({ select: { shop: true } });
  if (!session) throw new Error("No Session row — install the app first or pass --shop=");
  return session.shop;
}

async function main() {
  const shop = await resolveShop();
  // Unique per-run SKU so each live test is traceable in Postgres and never
  // collides with a previous run.
  const runId = Date.now().toString(36);
  const sku = `${PRODUCT.sku}-${runId}`;
  console.log(`→ Adding 1 product to ${shop} (store only, no triggers): ${sku}\n`);
  const { admin } = await shopify.unauthenticated.admin(shop);

  const createData = await gql(admin, PRODUCT_CREATE, {
    product: {
      title: PRODUCT.title,
      descriptionHtml: PRODUCT.descriptionHtml,
      vendor: PRODUCT.vendor,
      productType: PRODUCT.productType,
      tags: PRODUCT.tags,
      status: "ACTIVE",
    },
    media: [{ originalSource: PRODUCT.image, alt: PRODUCT.title, mediaContentType: "IMAGE" }],
  });

  const { product, userErrors } = createData.productCreate;
  if (userErrors.length) throw new Error(`productCreate errors: ${JSON.stringify(userErrors)}`);

  const variantId = product.variants.nodes[0]?.id;
  const updateData = await gql(admin, VARIANTS_BULK_UPDATE, {
    productId: product.id,
    variants: [{
      id: variantId,
      price: PRODUCT.price,
      compareAtPrice: PRODUCT.compareAtPrice,
      inventoryItem: { sku, tracked: false },
    }],
  });
  const vErrors = updateData.productVariantsBulkUpdate.userErrors;
  if (vErrors.length) console.error(`! variant update errors:`, vErrors);

  console.log(`✓ created in Shopify: ${product.id}  "${product.title}"  ₹${PRODUCT.price}  sku=${sku}`);
  console.log(`  (now watch Postgres — it should appear via the products/create webhook automatically)`);
  await prisma.$disconnect();
}

main().catch(async (err) => {
  console.error("Fatal:", err);
  await prisma.$disconnect();
  process.exit(1);
});
