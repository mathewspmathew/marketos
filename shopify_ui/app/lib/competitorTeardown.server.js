// shopify_ui/app/lib/competitorTeardown.server.js
/**
 * Guarded delete of a product's competitor data (chatbot disable->delete path).
 *
 * The set-resolution rule MUST stay identical to
 * services/chatbot_svc/tools/toggle_settings.py::compute_disable_counts, which
 * computes the counts shown on the card. A competitor ScrapedProduct is only
 * deleted for product P if no OTHER product references it (shared-row guard).
 *
 * We do NOT delete the merchant's own ShopifyProduct/Variant. Deleting a
 * ScrapedProduct cascades (DB-level) to its ScrapedVariants ->
 * CompetitorPriceObservation, ProductEmbedding, and competitor-side ProductMatch.
 */
export async function deleteCompetitorData(prisma, shopDomain, productId) {
  // The reads below run outside the $transaction intentionally: this fires only
  // when a merchant manually disables one product (with in-flight scraping
  // already being cancelled in the same flow), so the race window is negligible
  // and the worst case is a stray un-deleted ScrapedProduct, not corruption.
  const cands = await prisma.competitorCandidate.findMany({
    where: { shopDomain, shopifyProductId: productId },
    select: { scrapedProductId: true },
  });
  const urls = await prisma.productUrl.findMany({
    where: { shopDomain, shopifyProductId: productId },
    select: { prodId: true },
  });
  const myScraped = new Set([
    ...cands.map((c) => c.scrapedProductId).filter(Boolean),
    ...urls.map((u) => u.prodId).filter(Boolean),
  ]);

  const deletable = [];
  for (const sid of myScraped) {
    const otherCand = await prisma.competitorCandidate.findFirst({
      where: { scrapedProductId: sid, shopifyProductId: { not: productId } },
      select: { id: true },
    });
    const otherUrl = await prisma.productUrl.findFirst({
      where: { prodId: sid, shopifyProductId: { not: productId } },
      select: { id: true },
    });
    if (!otherCand && !otherUrl) deletable.push(sid);
  }

  const variants = await prisma.shopifyVariant.findMany({
    where: { productId },
    select: { id: true },
  });
  const variantIds = variants.map((v) => v.id);

  await prisma.$transaction(async (tx) => {
    if (variantIds.length) {
      await tx.variantCompetitorStats.deleteMany({ where: { shopifyVariantId: { in: variantIds } } });
      await tx.priceDecision.deleteMany({ where: { shopifyVariantId: { in: variantIds } } });
      await tx.productMatch.deleteMany({ where: { shopifyVariantId: { in: variantIds } } });
    }
    await tx.productSuggestion.deleteMany({ where: { shopifyProductId: productId } });
    await tx.productLevelMatch.deleteMany({ where: { shopifyProductId: productId } });
    await tx.competitorCandidate.deleteMany({ where: { shopDomain, shopifyProductId: productId } });
    await tx.discoveryJob.deleteMany({ where: { shopDomain, shopifyProductId: productId } });
    await tx.productUrl.deleteMany({ where: { shopDomain, shopifyProductId: productId } });
    if (deletable.length) {
      await tx.scrapedProduct.deleteMany({ where: { id: { in: deletable } } });
    }
  });

  return { deletedScrapedProducts: deletable.length };
}
