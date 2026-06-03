# services/scraper_svc/tests/test_scrape_candidate_cancel.py
import os
os.environ.setdefault("GROQ_API_KEY", "test")

import uuid
from unittest.mock import patch, MagicMock

from sqlalchemy import text

from services.common.db import get_db
from services.common.models import CompetitorCandidate
from services.scraper_svc.candidate import scrape_candidate


def test_scrape_candidate_skips_when_dynamic_pricing_off(seed_shop):
    """A candidate whose ShopifyProduct has dynamicPricingEnabled=False must
    abort before any Firecrawl call (cooperative cancellation)."""
    # seed_shop's product is created with dynamicPricingEnabled=False.
    with get_db() as s:
        product_id = s.execute(
            text('SELECT id FROM "ShopifyProduct" WHERE "shopDomain" = :d LIMIT 1'),
            {"d": seed_shop},
        ).scalar_one()

    cand_id = uuid.uuid4().hex
    with get_db() as s:
        s.add(CompetitorCandidate(
            id=cand_id, shopDomain=seed_shop, shopifyProductId=product_id,
            url="https://example.com/p/1", domain="example.com",
            source="test", status="PENDING",
        ))
    try:
        with patch("services.scraper_svc.candidate._firecrawl_client") as fc:
            fc.scrape_url = MagicMock()
            result = scrape_candidate.run(cand_id)
            assert result == {"status": "skipped_disabled"}
            fc.scrape_url.assert_not_called()
    finally:
        with get_db() as s:
            s.query(CompetitorCandidate).filter(CompetitorCandidate.id == cand_id).delete()
