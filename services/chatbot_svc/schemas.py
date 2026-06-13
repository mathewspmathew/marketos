from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field


class ScopeFilter(BaseModel):
    """Concrete predicate for selecting ShopifyVariant rows.

    Used by both preview and apply. Variants are resolved at preview time
    and frozen into ChatPreview.variantIds, so the apply uses the frozen
    set, not a re-evaluation of this filter.
    """
    vendor: Optional[str] = None
    product_type: Optional[str] = None
    title_contains: Optional[str] = None
    tags_any: Optional[list[str]] = None
    option_filters: Optional[dict[str, str]] = None
    dynamic_pricing_enabled: Optional[bool] = None
    variant_ids: Optional[list[str]] = None
    product_ids: Optional[list[str]] = None


class PriceChange(BaseModel):
    type: Literal["percent", "absolute", "set"]
    value: float = Field(..., description="percent: e.g. 10 means +10%. absolute: currency delta. set: new price.")


class FlagChange(BaseModel):
    enabled: bool


class VariantSummary(BaseModel):
    variant_id: str
    product_id: str
    title: str
    vendor: Optional[str]
    current_price: float
    dynamic_pricing_enabled: bool


class ResolvedProduct(BaseModel):
    """A real product matched from a free-text reference (product-level)."""
    product_id: str
    title: str
    vendor: Optional[str] = None
    variant_ids: list[str]
    dynamic_pricing_enabled: bool
    fuzzy: bool = False  # True when matched by similarity (typo/partial), not exact title
    score: float = 1.0  # match confidence: 1.0 exact, else trigram word_similarity
    weak: bool = False  # True = only a loose/low-confidence name match; confirm before acting


class DynamicPricingStatus(BaseModel):
    """Where a product stands in the dynamic-pricing pipeline (merchant-facing)."""
    product_id: str
    title: str
    status: Literal["OFF", "SETTING_UP", "DISCOVERING", "PROCESSING", "READY", "NEEDS_ATTENTION"]
    competitors_found: int
    matches: int
    last_discovery_at: Optional[str] = None
    detail: str


class EnableContext(BaseModel):
    """History of a product's dynamic-pricing setup, used to shape the enable card.

    state:
      FRESH            — flag off and no CompetitorCandidate rows (never set up)
      PAUSED_WITH_DATA — flag off but candidate rows exist (paused, data kept)
      ACTIVE           — dynamicPricingEnabled is already true
    """
    product_id: str
    state: Literal["FRESH", "PAUSED_WITH_DATA", "ACTIVE"]
    competitors_found: int = 0
    live_matches: int = 0
    last_discovery_at: Optional[str] = None
    existing_query: Optional[str] = None
    current_query: str = ""
    query_drifted: bool = False
    dead_links: int = 0
    num_results: int = 10
    listing_expansion_cap: int = 5


class QueryCandidate(BaseModel):
    """One proposed competitor-search query with a rough self-confidence."""
    query: str
    confidence: int = Field(ge=0, le=10, description="0-10 rough belief it surfaces real competitors")
    reason: str


class PreviewSummary(BaseModel):
    preview_id: str
    kind: Literal["price_change", "dynamic_pricing_toggle"]
    count: int
    sample_rows: list[VariantSummary]
    min_new: Optional[float] = None
    max_new: Optional[float] = None
    avg_new: Optional[float] = None
    revenue_delta_est: Optional[float] = None
    human_summary: str
    expires_at: str


class ApplyResult(BaseModel):
    preview_id: str
    succeeded: list[str]
    failed: list[dict]


class ChatEvent(BaseModel):
    type: Literal["text", "tool_start", "tool_end", "ask", "preview", "applied", "done", "error"]
    data: dict
