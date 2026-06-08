/**
 * scripts/seed-products.js — seed dummy stationery products into the dev store
 *
 * Creates 5 real, searchable Indian-market stationery products (with images and
 * INR prices) in the connected Shopify dev store via the Admin GraphQL API.
 * Creating them through the Admin API fires the `products/create` webhook, so the
 * app syncs them into Postgres and the semantic → embedding pipeline kicks off
 * automatically. Turn on dynamic pricing per product afterwards to drive
 * discovery → scrape → match → stats → pricing.
 *
 * Auth mirrors app/routes/internal.apply-price.jsx: it uses
 * `shopify.unauthenticated.admin(shop)`, which reads the offline token from the
 * Session table and refreshes it via Token Exchange when needed. The app must be
 * installed on the shop (a Session row must exist) before running this.
 *
 * Run from shopify_ui/ with the same env the app uses:
 *
 *   node --env-file=.env scripts/seed-products.js [--shop=your-store.myshopify.com]
 *
 * If --shop is omitted, the first shop in the Session table is used.
 * NOTE: re-running creates duplicates — it does not upsert.
 */
// Self-contained Shopify app setup (mirrors app/shopify.server.js). We do NOT
// import app/shopify.server.js because it uses extensionless imports that Vite
// resolves but plain Node does not.
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
  // appUrl is irrelevant for unauthenticated.admin token exchange; the library
  // only requires it to be non-empty. CLI normally injects SHOPIFY_APP_URL.
  appUrl: process.env.SHOPIFY_APP_URL || process.env.WEBHOOK_BASE_URL || "https://localhost",
  authPathPrefix: "/auth",
  sessionStorage: new PrismaSessionStorage(prisma),
  distribution: AppDistribution.AppStore,
  future: { expiringOfflineAccessTokens: true },
});

// Store price is set slightly off the typical market price on purpose, so the
// comparison/pricing stage has room to move it. compareAtPrice shows the
// "normal" price for the strikethrough.
const PRODUCTS = [
  {
    title: "Classmate Short Size Notebook 19x15.5cm Single Line Ruled - 172 Pages, Pack of 5",
    vendor: "Classmate",
    productType: "Notebooks",
    tags: ["stationery", "school", "kids", "notebook", "single-line"],
    descriptionHtml:
      "<p>Compact 19x15.5cm size designed for younger students. Whiter, brighter, smooth smudge-free paper with sturdy binding that lasts the school year. Illustrated contemporary covers with fun facts, index page, notebook label, page numbers and Edugames activities. Elemental-chlorine-free, eco-friendly paper.</p><ul><li>Pack of 5 notebooks, 172 pages each</li><li>Single line ruled</li><li>Compact short size for primary classes</li></ul>",
    image: "https://m.media-amazon.com/images/I/7190tIKKXqL._SL1000_.jpg",
    price: "145.00",
    compareAtPrice: "180.00",
    sku: "CM-NB-SL-172-5",
  },
  {
    title: "Camlin Scholar Pro Geometry Box - 12-Piece Set",
    vendor: "Camlin",
    productType: "Geometry Sets",
    tags: ["stationery", "school", "geometry", "maths"],
    descriptionHtml:
      "<p>Premium 12-piece geometry kit for school students. High-precision compass for accurate constructions, set squares, protractor, mechanical pencil and more. Meticulously calibrated tools, sleek modern design and an organized layout that keeps everything in place.</p><ul><li>12 essential geometry instruments</li><li>High-precision compass</li><li>Pack of 1</li></ul>",
    image: "https://m.media-amazon.com/images/I/713nJhVcvNL._SL1000_.jpg",
    price: "339.00",
    compareAtPrice: "410.00",
    sku: "CAM-GEO-SCH-12",
  },
  {
    title: "Pilot V5 Hi-Tecpoint Roller Ball Pen 0.5mm - 2 Pens + 4 Blue Cartridges",
    vendor: "Pilot",
    productType: "Pens",
    tags: ["stationery", "office", "pen", "rollerball"],
    descriptionHtml:
      "<p>Japanese 3-dimple tip technology for precision writing and an ATT system for instant start. Pure liquid ink delivers smooth, skip-free writing with a 0.5mm fine tip in blue. Begreen eco-friendly construction.</p><ul><li>0.5mm fine tip, blue ink</li><li>Refillable cartridge system</li><li>Includes 2 pens + 4 blue cartridges</li></ul>",
    image: "https://m.media-amazon.com/images/I/51RZHP7lknL._SL1000_.jpg",
    price: "175.00",
    compareAtPrice: "200.00",
    sku: "PIL-V5-2P4C",
  },
  {
    title: "Faber-Castell Triangular Colour Pencils - Pack of 24",
    vendor: "Faber-Castell",
    productType: "Art Supplies",
    tags: ["stationery", "school", "kids", "colour-pencils", "art"],
    descriptionHtml:
      "<p>24 vibrant triangular colour pencils with an ergonomic grip designed for kids aged 6-12. Richly pigmented leads give smooth, even colour on all paper types, and the break-resistant build withstands daily school use.</p><ul><li>24 assorted colours</li><li>Triangular shape for better grip</li><li>Ideal for school and art projects</li></ul>",
    image: "https://m.media-amazon.com/images/I/812NT72OnKL._SL1000_.jpg",
    price: "185.00",
    compareAtPrice: "220.00",
    sku: "FC-CP-TRI-24",
  },
  {
    title: "Fevicol MR Adhesive Glue for Arts & Crafts - 200 g",
    vendor: "Fevicol",
    productType: "Adhesives",
    tags: ["stationery", "school", "glue", "adhesive", "craft"],
    descriptionHtml:
      "<p>Classic white multi-surface craft adhesive. Easy to use and ideal for school projects, arts and crafts. Non-toxic and safe for children.</p><ul><li>200 g pack</li><li>Multi-surface craft glue</li><li>Perfect for school project work</li></ul>",
    image: "https://m.media-amazon.com/images/I/31d%2BG3bWT2L._SL1000_.jpg",
    price: "145.00",
    compareAtPrice: "165.00",
    sku: "FEV-MR-200G",
  },
];

const PRODUCT_CREATE = `
  mutation seedProductCreate($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
    productCreate(product: $product, media: $media) {
      product {
        id
        title
        variants(first: 1) { nodes { id } }
      }
      userErrors { field message }
    }
  }
`;

const VARIANTS_BULK_UPDATE = `
  mutation seedVariantUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
    productVariantsBulkUpdate(productId: $productId, variants: $variants) {
      productVariants { id price compareAtPrice sku }
      userErrors { field message }
    }
  }
`;

async function gql(admin, query, variables) {
  const resp = await admin.graphql(query, { variables });
  const body = await resp.json();
  if (body.errors) {
    throw new Error(`GraphQL transport error: ${JSON.stringify(body.errors)}`);
  }
  return body.data;
}

function getShopArg() {
  const flag = process.argv.find((a) => a.startsWith("--shop="));
  return flag ? flag.split("=")[1] : process.env.SHOP || null;
}

async function resolveShop() {
  const arg = getShopArg();
  if (arg) return arg;
  const session = await prisma.session.findFirst({ select: { shop: true } });
  if (!session) {
    throw new Error(
      "No Session row found — install the app on a dev store first, or pass --shop=your-store.myshopify.com",
    );
  }
  return session.shop;
}

async function main() {
  const shop = await resolveShop();
  console.log(`→ Seeding ${PRODUCTS.length} products into ${shop}\n`);

  const { admin } = await shopify.unauthenticated.admin(shop);

  let created = 0;
  for (const p of PRODUCTS) {
    try {
      const createData = await gql(admin, PRODUCT_CREATE, {
        product: {
          title: p.title,
          descriptionHtml: p.descriptionHtml,
          vendor: p.vendor,
          productType: p.productType,
          tags: p.tags,
          status: "ACTIVE",
        },
        media: [
          {
            originalSource: p.image,
            alt: p.title,
            mediaContentType: "IMAGE",
          },
        ],
      });

      const { product, userErrors } = createData.productCreate;
      if (userErrors.length) {
        console.error(`✗ ${p.sku}: productCreate errors:`, userErrors);
        continue;
      }

      const variantId = product.variants.nodes[0]?.id;
      const updateData = await gql(admin, VARIANTS_BULK_UPDATE, {
        productId: product.id,
        variants: [
          {
            id: variantId,
            price: p.price,
            compareAtPrice: p.compareAtPrice,
            // tracked:false keeps the variant sellable without a location-scoped
            // inventory quantity, which is all the pipeline needs (isInStock).
            inventoryItem: { sku: p.sku, tracked: false },
          },
        ],
      });

      const vErrors = updateData.productVariantsBulkUpdate.userErrors;
      if (vErrors.length) {
        console.error(`! ${p.sku}: variant update errors:`, vErrors);
      }

      created += 1;
      console.log(`✓ ${p.sku.padEnd(16)} ₹${p.price}  ${product.id}  "${product.title}"`);
    } catch (err) {
      console.error(`✗ ${p.sku}: ${err.message}`);
    }
  }

  console.log(`\nDone: ${created}/${PRODUCTS.length} products created.`);
  console.log(
    "Next: in the MarketOS app, toggle dynamicPricingEnabled ON per product and set floor/ceiling to drive the pipeline.",
  );
  await prisma.$disconnect();
}

main().catch(async (err) => {
  console.error("Fatal:", err);
  await prisma.$disconnect();
  process.exit(1);
});
