"""Verify ShopifyProduct's pricing-pane columns are mapped on the SQLAlchemy model.

These columns already exist in Postgres (shopify_ui/prisma/schema.prisma:388-391)
but were missing from services/common/models.py — see
docs/superpowers/specs/2026-07-13-smoke-pipeline-pane-config-design.md.
"""
from services.common.models import ShopifyProduct


def test_shopify_product_has_pricing_tier_and_bounds():
    cols = {c.name for c in ShopifyProduct.__table__.columns}
    assert "pricingTier" in cols
    assert "minPriceOverride" in cols
    assert "maxPriceOverride" in cols
