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
