"""services/pricing_svc/match_review.py

Confirm or reject a merchant-competitor match (ProductLevelMatch). The one
place this decision is written — both app.matches.jsx's action and (in the
future) any other caller go through the /internal/matches/review endpoint,
which wraps these two functions.

Confirming a LIKELY match no longer needs to force-upgrade confidenceTier to
CONFIRMED — decide.py's eligibility gate already counts a LIKELY match with
reviewStatus=CONFIRMED on its own.

Confirming also fans out stats.recompute_for_variant for the match's
variants, since decide.py's gate only ever runs off a scrape-triggered
recompute otherwise — without this, a merchant could confirm matches and
never see a price decision until the next scrape happens to land.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from services.common import models
from services.common.celery_app import app


class MatchReviewError(ValueError):
    """Raised when a confirm/reject request fails validation."""


def _get_owned_match(session: Session, shop_domain: str, match_id: str) -> models.ProductLevelMatch:
    match = session.get(models.ProductLevelMatch, match_id)
    if match is None or match.shopDomain != shop_domain:
        raise MatchReviewError(f"Match {match_id} not found for this shop.")
    return match


def confirm_match(session: Session, shop_domain: str, match_id: str) -> dict:
    match = _get_owned_match(session, shop_domain, match_id)
    if match.reviewStatus != "CONFIRMED":
        match.reviewStatus = "CONFIRMED"
        match.reviewedAt = datetime.now(timezone.utc)
        session.flush()
        variant_ids = [
            row[0] for row in session.query(models.ProductMatch.shopifyVariantId)
            .filter(models.ProductMatch.productMatchId == match_id)
            .distinct()
        ]
        for variant_id in variant_ids:
            app.send_task(
                "stats.recompute_for_variant",
                args=[shop_domain, variant_id],
                queue="stats_queue",
            )
    return {"matchId": match_id, "reviewStatus": "CONFIRMED"}


def reject_match(session: Session, shop_domain: str, match_id: str) -> dict:
    match = _get_owned_match(session, shop_domain, match_id)
    if match.reviewStatus != "REJECTED":
        match.reviewStatus = "REJECTED"
        match.reviewedAt = datetime.now(timezone.utc)
        session.query(models.ProductMatch).filter(
            models.ProductMatch.productMatchId == match_id,
        ).delete(synchronize_session=False)
    return {"matchId": match_id, "reviewStatus": "REJECTED"}
