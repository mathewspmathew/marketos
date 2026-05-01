from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class VariantSchema(BaseModel):
    sku: Optional[str] = Field(None)
    barcode: Optional[str] = Field(None)
    title: str = Field("Default Title")
    options: Optional[Dict[str, str]] = Field(None)
    current_price: float = Field(...)
    original_price: Optional[float] = Field(None)
    is_in_stock: bool = Field(True)
    stock_quantity: Optional[int] = Field(None)

class ProductSchema(BaseModel):
    title: Optional[str] = Field(None)
    description: Optional[str] = Field(None)
    vendor: Optional[str] = Field(None)
    product_type: Optional[str] = Field(None)
    tags: List[str] = Field(default_factory=list)
    image_url: Optional[str] = Field(None)
    specifications: Optional[Dict[str, Any]] = Field(None)
    variants: List[VariantSchema] = Field(default_factory=list)

