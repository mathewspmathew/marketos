from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


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


class PaneConfigInput(BaseModel):
    """Chat-extracted dynamic-pricing pane configuration. EVERY field below is
    REQUIRED in the tool call — you must include all of them. Set a field to
    null for anything the user did NOT specify in their message; null means
    "keep the product's current value" (apply_pane_config's existing
    "None = unchanged" semantics), it does NOT reset the field to a default.
    Only set a field to a real value when the user actually gave one. Do not
    invent, guess, or infer values, and do not use any field names other than
    the ones listed here — an unrecognized field name will be rejected."""
    model_config = ConfigDict(extra="forbid")

    search_query_override: Optional[str] = Field(..., description="null unless the user gave a search query.")
    pricing_tier: Optional[Literal["BUDGET", "COMPETITIVE", "PREMIUM"]] = Field(..., description="null unless the user named a tier.")
    min_price_override: Optional[float] = Field(..., description="null unless the user gave a minimum price.")
    max_price_override: Optional[float] = Field(..., description="null unless the user gave a maximum price.")
    frequency_unit: Optional[Literal["never", "minute", "hour", "day"]] = Field(..., description="null unless the user gave a rescrape unit.")
    frequency_interval: Optional[int] = Field(..., description="null unless the user gave a rescrape interval number.")
    discovery_num_results: Optional[int] = Field(..., description="null unless the user gave a competitor-products-per-run count.")
    listing_expansion_cap: Optional[int] = Field(..., description="null unless the user gave a products-per-listing-page count.")


class ApplyPaneConfigResult(BaseModel):
    product_id: str
    product_title: str
    dynamic_pricing_enabled_before: bool
    dynamic_pricing_enabled_after: bool
    rearmed_count: int
    human_summary: str


class PauseDynamicPricingResult(BaseModel):
    product_id: str
    product_title: str
    dynamic_pricing_enabled_before: bool
    dynamic_pricing_enabled_after: bool
    human_summary: str


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


class PanelSummary(BaseModel):
    """What the agent sees after opening the dynamic-pricing panel for one product.

    The full card payload (form defaults, counts, product info) lives in the
    ChatPreview row and reaches the browser via the SSE preview event; the
    agent only needs enough to phrase a 1-2 sentence reply."""
    preview_id: str
    kind: Literal["dynamic_pricing_toggle"] = "dynamic_pricing_toggle"
    card_state: Literal["FRESH", "ACTIVE", "PAUSED"]
    product_id: str
    product_title: str
    human_summary: str
    expires_at: str


class ApplyResult(BaseModel):
    preview_id: str
    succeeded: list[str]
    failed: list[dict]


class ChatEvent(BaseModel):
    type: Literal["text", "tool_start", "tool_end", "ask", "preview", "applied", "done", "error"]
    data: dict


class CandidateCountByStatus(BaseModel):
    """Breakdown of competitor candidates by pipeline status."""
    pending: int
    scraped: int
    verified: int
    rejected: int
    dead: int


class DiscoveryDebugInfo(BaseModel):
    """Detailed discovery pipeline diagnostics for a product."""
    product_id: str
    product_title: str
    latest_job_id: str
    job_status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]
    query_used: str
    num_results_requested: int
    listing_expansion_cap: int
    job_requested_at: str
    job_completed_at: Optional[str] = None
    candidate_counts: CandidateCountByStatus
    total_candidates_found: int
    verified_candidates: int
    matched_variants: int
    unmatched_verified: int
    job_error: Optional[str] = None
    hint: str
    recommended_action: str


class CompetitorPricingContext(BaseModel):
    """Competitor price statistics for a variant."""
    median: float
    mean: float
    min_price: float
    max_price: float
    count: int
    last_observed: Optional[str] = None


class PriceExplanation(BaseModel):
    """Explanation of why a price was recommended for a variant."""
    variant_id: str
    variant_title: str
    current_price: float
    recommended_price: float
    price_delta: float
    delta_percent: float
    decided_at: str
    competitor_pricing: Optional[CompetitorPricingContext] = None
    primary_factor: str
    explanation: str


class MatchExplanation(BaseModel):
    """Explanation of how a competitor variant was matched to a merchant variant."""
    variant_id: str
    variant_title: str
    competitor_variant_title: str
    competitor_url: Optional[str] = None
    confidence_tier: Literal["CONFIRMED", "LIKELY", "WEAK"]
    match_score: float
    vector_distance: float
    matched_at: str
    explanation: str
    other_candidates_evaluated: int
    recommended_action: str
