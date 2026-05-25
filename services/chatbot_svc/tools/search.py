from __future__ import annotations

from sqlalchemy import select, text as sa_text
from sqlalchemy.dialects.postgresql import JSONB

from services.common.db import get_db
from services.common.models import ShopifyProduct, ShopifyVariant
from services.chatbot_svc.schemas import ScopeFilter, VariantSummary
from services.common.vertex_embed import embed_text as _embed_text


def structured_search(
    shop_domain: str,
    sf: ScopeFilter,
    limit: int = 25,
    offset: int = 0,
) -> list[VariantSummary]:
    """Return ShopifyVariant rows matching the filter, scoped to shop_domain.

    ShopifyVariant has no shopDomain column — isolation is enforced via
    the JOIN on ShopifyProduct.shopDomain.
    """
    with get_db() as s:
        stmt = (
            select(ShopifyVariant, ShopifyProduct)
            .join(ShopifyProduct, ShopifyProduct.id == ShopifyVariant.productId)
            .where(ShopifyProduct.shopDomain == shop_domain)
        )

        if sf.vendor:
            stmt = stmt.where(ShopifyProduct.vendor == sf.vendor)
        if sf.product_type:
            stmt = stmt.where(ShopifyProduct.productType == sf.product_type)
        if sf.title_contains:
            stmt = stmt.where(ShopifyProduct.title.ilike(f"%{sf.title_contains}%"))
        if sf.tags_any:
            # tags column is JSONB array; ?| checks if any of the given keys exist
            stmt = stmt.where(ShopifyProduct.tags.cast(JSONB).op("?|")(sf.tags_any))
        if sf.option_filters:
            for k, v in sf.option_filters.items():
                stmt = stmt.where(ShopifyVariant.options[k].astext.ilike(v))
        if sf.dynamic_pricing_enabled is not None:
            stmt = stmt.where(
                ShopifyProduct.dynamicPricingEnabled == sf.dynamic_pricing_enabled
            )
        if sf.variant_ids:
            stmt = stmt.where(ShopifyVariant.id.in_(sf.variant_ids))
        if sf.product_ids:
            stmt = stmt.where(ShopifyProduct.id.in_(sf.product_ids))

        stmt = stmt.limit(limit).offset(offset)

        out: list[VariantSummary] = []
        for v, p in s.execute(stmt).all(
            
        ):
            out.append(
                VariantSummary(
                    variant_id=v.id,
                    product_id=p.id,
                    title=p.title,
                    vendor=p.vendor,
                    current_price=float(v.currentPrice or 0),
                    dynamic_pricing_enabled=bool(p.dynamicPricingEnabled),
                )
            )
        return out


def semantic_search(shop_domain: str, query: str, top_k: int = 20) -> list[VariantSummary]:
    """Vector similarity search against ShopifyEmbedding.vectorText, scoped to shop_domain."""
    vec = _embed_text(query)
    vec_lit = "[" + ",".join(str(x) for x in vec) + "]"
    sql = sa_text("""
        SELECT v.id AS variant_id, p.id AS product_id, p.title, p.vendor,
               COALESCE(v."currentPrice", 0) AS price,
               COALESCE(p."dynamicPricingEnabled", FALSE) AS dpe
        FROM "ShopifyEmbedding" e
        JOIN "ShopifyVariant" v ON v.id = e."variantId"
        JOIN "ShopifyProduct" p ON p.id = v."productId"
        WHERE p."shopDomain" = :shop
          AND e."vectorText" IS NOT NULL
        ORDER BY e."vectorText" <=> (:vec)::vector
        LIMIT :k
    """)
    with get_db() as s:
        rows = s.execute(sql, {"shop": shop_domain, "vec": vec_lit, "k": top_k}).mappings().all()
    return [
        VariantSummary(
            variant_id=r["variant_id"],
            product_id=r["product_id"],
            title=r["title"],
            vendor=r["vendor"],
            current_price=float(r["price"]),
            dynamic_pricing_enabled=bool(r["dpe"]),
        )
        for r in rows
    ]


def get_variant(shop_domain: str, variant_id: str) -> VariantSummary | None:
    """Fetch a single variant by id, scoped to shop_domain (JOIN via ShopifyProduct)."""
    with get_db() as s:
        stmt = (
            select(ShopifyVariant, ShopifyProduct)
            .join(ShopifyProduct, ShopifyProduct.id == ShopifyVariant.productId)
            .where(
                ShopifyVariant.id == variant_id,
                ShopifyProduct.shopDomain == shop_domain,
            )
        )
        row = s.execute(stmt).first()
        if not row:
            return None
        v, p = row
        return VariantSummary(
            variant_id=v.id,
            product_id=p.id,
            title=p.title,
            vendor=p.vendor,
            current_price=float(v.currentPrice or 0),
            dynamic_pricing_enabled=bool(p.dynamicPricingEnabled),
        )
