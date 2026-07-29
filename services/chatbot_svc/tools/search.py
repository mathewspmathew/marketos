from __future__ import annotations

from sqlalchemy import func, select, text as sa_text
from sqlalchemy.dialects.postgresql import JSONB

from services.common.db import get_db
from services.common.models import ShopifyProduct, ShopifyVariant, ShopSettings
from services.chatbot_svc.schemas import ScopeFilter, VariantSummary, ResolvedProduct
from services.common.vertex_embed import embed_text as _embed_text

DEFAULT_CURRENCY = "INR"


def _shop_currency(s, shop_domain: str) -> str:
    """Shop's configured currency, falling back to INR (this deployment's default)."""
    settings = s.get(ShopSettings, shop_domain)
    return (settings.currency if settings and settings.currency else DEFAULT_CURRENCY)


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

        currency = _shop_currency(s, shop_domain)
        out: list[VariantSummary] = []
        for v, p in s.execute(stmt).all():
            out.append(
                VariantSummary(
                    variant_id=v.id,
                    product_id=p.id,
                    title=p.title,
                    vendor=p.vendor,
                    current_price=float(v.currentPrice or 0),
                    currency=currency,
                    dynamic_pricing_enabled=bool(p.dynamicPricingEnabled),
                )
            )
        return out


def semantic_search(shop_domain: str, query: str, top_k: int = 20) -> list[VariantSummary]:
    """Vector similarity search against ShopifyEmbedding.vectorText, scoped to shop_domain."""
    vec = _embed_text(query)
    if vec is None:
        # Embedding failed (e.g. missing Vertex credentials) or query was blank.
        # Surface a legible error instead of crashing on iteration over None;
        # the message nudges the agent to fall back to structured_search.
        raise RuntimeError(
            "semantic search unavailable: text embedding failed "
            "(check Vertex credentials). Use structured_search instead."
        )
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
        currency = _shop_currency(s, shop_domain)
    return [
        VariantSummary(
            variant_id=r["variant_id"],
            product_id=r["product_id"],
            title=r["title"],
            vendor=r["vendor"],
            current_price=float(r["price"]),
            currency=currency,
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
            currency=_shop_currency(s, shop_domain),
            dynamic_pricing_enabled=bool(p.dynamicPricingEnabled),
        )


STRONG_SIM = 0.5   # best word_similarity >= this -> confident match
WEAK_FLOOR = 0.23  # below this -> not a match at all (drops nonsense trigram noise ~0.21)


def _to_resolved(s, p, *, score: float, fuzzy: bool, weak: bool) -> ResolvedProduct:
    vids = (
        s.execute(select(ShopifyVariant.id).where(ShopifyVariant.productId == p.id))
        .scalars()
        .all()
    )
    return ResolvedProduct(
        product_id=p.id,
        title=p.title,
        vendor=p.vendor,
        variant_ids=list(vids),
        dynamic_pricing_enabled=bool(p.dynamicPricingEnabled),
        fuzzy=fuzzy,
        score=round(float(score), 3),
        weak=weak,
    )


def resolve_product(
    shop_domain: str,
    reference: str,
    limit: int = 10,
    *,
    strong_sim: float = STRONG_SIM,
    weak_floor: float = WEAK_FLOOR,
) -> list[ResolvedProduct]:
    """Resolve a free-text product reference to real ShopifyProducts in this shop.

    Tiers by trigram word_similarity (the match's confidence `score`):
      - exact (case-insensitive) title  -> score 1.0, weak=False, fuzzy=False
      - best similarity >= strong_sim    -> confident matches (>= strong_sim), weak=False, fuzzy=True
      - best in [weak_floor, strong_sim) -> low-confidence guesses (all >= weak_floor), weak=True, fuzzy=True
      - nothing >= weak_floor            -> [] (not found)

    Shop-scoped: a reference can never resolve to another merchant's product.
    """
    ref = (reference or "").strip()
    if not ref:
        return []

    with get_db() as s:
        exact = (
            s.execute(
                select(ShopifyProduct).where(
                    ShopifyProduct.shopDomain == shop_domain,
                    func.lower(ShopifyProduct.title) == ref.lower(),
                )
            )
            .scalars()
            .all()
        )
        if exact:
            return [_to_resolved(s, p, score=1.0, fuzzy=False, weak=False) for p in exact]

        sim_sql = sa_text(
            """
            SELECT id, word_similarity(lower(:ref), lower(title)) AS sim
            FROM "ShopifyProduct"
            WHERE "shopDomain" = :shop
              AND word_similarity(lower(:ref), lower(title)) >= :floor
            ORDER BY sim DESC
            LIMIT :lim
            """
        )
        rows = s.execute(
            sim_sql,
            {"ref": ref, "shop": shop_domain, "floor": weak_floor, "lim": limit},
        ).all()
        if not rows:
            return []

        best = rows[0][1]
        if best >= strong_sim:
            kept = [(pid, sim) for (pid, sim) in rows if sim >= strong_sim]
            weak = False
        else:
            kept = [(pid, sim) for (pid, sim) in rows]
            weak = True

        out: list[ResolvedProduct] = []
        for pid, sim in kept:
            p = s.get(ShopifyProduct, pid)
            if p is not None:
                out.append(_to_resolved(s, p, score=float(sim), fuzzy=True, weak=weak))
        return out
