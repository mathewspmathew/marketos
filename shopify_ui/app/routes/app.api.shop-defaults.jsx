import { authenticate } from "../shopify.server";
import db from "../db.server";

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  try {
    const shopSettings = await db.shopSettings.findUnique({
      where: { shopDomain },
      select: {
        frequencyInterval: true,
        frequencyUnit: true,
        listingExpansionCap: true,
        discoveryNumResults: true,
      },
    });

    if (!shopSettings) {
      return Response.json(
        { error: "Shop settings not found" },
        { status: 404 }
      );
    }

    return Response.json(shopSettings);
  } catch (error) {
    console.error("Error fetching shop defaults:", error);
    return Response.json(
      { error: "Failed to fetch shop defaults" },
      { status: 500 }
    );
  }
};
