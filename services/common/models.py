"""
services/common/models.py

SQLAlchemy ORM models mirroring the Prisma schema exactly.
Prisma owns migrations — SQLAlchemy never creates/alters tables.

Tenant key: ShopifyUser.shopDomain (the Shopify store domain, e.g. "mystore.myshopify.com").
All competitor data and configs hang off shopDomain, not a UUID user id.

Vector columns (ProductEmbedding, ShopifyVectorized) are omitted from the ORM
because SQLAlchemy has no native pgvector type; all vector reads/writes use raw SQL.
"""
import uuid

from sqlalchemy import BIGINT, Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM as PgEnum, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


_scrape_status = PgEnum(
    "IDLE", "QUEUED", "RUNNING", "SCRAPED_FIRST",
    name="ScrapeStatus",
    create_type=False,
)

_url_status = PgEnum(
    "ACTIVE", "DEAD", "PAUSED",
    name="UrlStatus",
    create_type=False,
)


# ─────────────────────────────────────────────────────────────────────────────
# Multi-tenancy root — one row per installed Shopify store
# ─────────────────────────────────────────────────────────────────────────────

class ShopifyUser(Base):
    __tablename__ = "ShopifyUser"

    shopDomain    = Column("shopDomain",    String, primary_key=True)
    shopifyUserId = Column("shopifyUserId", BIGINT)
    email         = Column("email",         String)
    firstName     = Column("firstName",     String)
    installedAt   = Column("installedAt",   DateTime(timezone=True), server_default=func.now())

    scrapingConfigs   = relationship("ScrapingConfig",   back_populates="shop")
    scrapedProducts   = relationship("ScrapedProduct",   back_populates="shop")
    productUrls       = relationship("ProductUrl",        back_populates="shop")
    productEmbeddings = relationship("ProductEmbedding",  back_populates="shop")
    scrapingErrors    = relationship("ScrapingError",     back_populates="shop")


# ─────────────────────────────────────────────────────────────────────────────
# Scraping configuration
# ─────────────────────────────────────────────────────────────────────────────

class ScrapingConfig(Base):
    __tablename__ = "ScrapingConfig"

    id                = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain        = Column("shopDomain",        String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    competitorUrl     = Column("competitorUrl",     String, nullable=False)
    includeImages     = Column("includeImages",     Boolean, default=True)
    productLimit      = Column("productLimit",      Integer)
    frequencyInterval = Column("frequencyInterval", Integer)
    nextRunAt         = Column("nextRunAt",         DateTime(timezone=True))
    isActive          = Column("isActive",          Boolean, default=True)
    status            = Column("status",            _scrape_status, nullable=False, default="IDLE")
    createdAt         = Column("createdAt",         DateTime(timezone=True), server_default=func.now())
    updatedAt         = Column("updatedAt",         DateTime(timezone=True), default=func.now(), onupdate=func.now())

    shop         = relationship("ShopifyUser",  back_populates="scrapingConfigs")
    product_urls = relationship("ProductUrl",   back_populates="config")
    errors       = relationship("ScrapingError", back_populates="config")


# ─────────────────────────────────────────────────────────────────────────────
# Competitor scraped data
# ─────────────────────────────────────────────────────────────────────────────

class ScrapedProduct(Base):
    __tablename__ = "ScrapedProduct"

    id             = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain     = Column("shopDomain",     String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    domain         = Column("domain",         String, nullable=False)
    title          = Column("title",          String, nullable=False)
    description    = Column("description",    Text)
    vendor         = Column("vendor",         String)
    productType    = Column("productType",    String)
    tags           = Column("tags",           JSONB, default=list)
    imageUrl       = Column("imageUrl",       String)
    specifications = Column("specifications", JSONB)
    createdAt      = Column("createdAt",      DateTime(timezone=True), server_default=func.now())
    updatedAt      = Column("updatedAt",      DateTime(timezone=True), default=func.now(), onupdate=func.now())

    shop       = relationship("ShopifyUser",    back_populates="scrapedProducts")
    variants   = relationship("ScrapedVariant", back_populates="product", cascade="all, delete-orphan")
    urls       = relationship("ProductUrl",      back_populates="product", cascade="all, delete-orphan")
    embeddings = relationship("ProductEmbedding", back_populates="product", cascade="all, delete-orphan")


class ScrapedVariant(Base):
    __tablename__ = "ScrapedVariant"

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    productId     = Column("productId",    String, ForeignKey("ScrapedProduct.id", ondelete="CASCADE"), nullable=False)
    sku           = Column("sku",          String)
    barcode       = Column("barcode",      String)
    title         = Column("title",        String, nullable=False, default="Default Title")
    options       = Column("options",      JSONB)
    currentPrice  = Column("currentPrice", Numeric(10, 2), nullable=False)
    originalPrice = Column("originalPrice",Numeric(10, 2))
    isInStock     = Column("isInStock",    Boolean, default=True)
    stockQuantity = Column("stockQuantity",Integer)
    semanticText  = Column("semanticText", Text)
    createdAt     = Column("createdAt",    DateTime(timezone=True), server_default=func.now())
    updatedAt     = Column("updatedAt",    DateTime(timezone=True), default=func.now(), onupdate=func.now())

    product    = relationship("ScrapedProduct",  back_populates="variants")
    embeddings = relationship("ProductEmbedding", back_populates="variant", cascade="all, delete-orphan")


# ─────────────────────────────────────────────────────────────────────────────
# URL lifecycle tracking
# ─────────────────────────────────────────────────────────────────────────────

class ProductUrl(Base):
    __tablename__ = "ProductUrl"

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain    = Column("shopDomain",    String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    configId      = Column("configId",      String, ForeignKey("ScrapingConfig.id"), nullable=False)
    prodId        = Column("prodId",        String, ForeignKey("ScrapedProduct.id", ondelete="CASCADE"), nullable=False)
    url           = Column("url",           String, unique=True, nullable=False)
    status        = Column("status",        _url_status, nullable=False, default="ACTIVE")
    failCount     = Column("failCount",     Integer, default=0)
    lastScrapedAt = Column("lastScrapedAt", DateTime(timezone=True))
    nextScrapAt   = Column("nextScrapAt",   DateTime(timezone=True))
    createdAt     = Column("createdAt",     DateTime(timezone=True), server_default=func.now())

    shop    = relationship("ShopifyUser",    back_populates="productUrls")
    config  = relationship("ScrapingConfig", back_populates="product_urls")
    product = relationship("ScrapedProduct", back_populates="urls")


# ─────────────────────────────────────────────────────────────────────────────
# Competitor variant vector embeddings
# ─────────────────────────────────────────────────────────────────────────────

class ProductEmbedding(Base):
    __tablename__ = "ProductEmbedding"

    id             = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain     = Column("shopDomain",     String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    prodId         = Column("prodId",         String, ForeignKey("ScrapedProduct.id", ondelete="CASCADE"), nullable=False)
    variantId      = Column("variantId",      String, ForeignKey("ScrapedVariant.id",  ondelete="SET NULL"), nullable=True)
    embeddingModel = Column("embeddingModel", String, nullable=False)
    vectorizedAt   = Column("vectorizedAt",   DateTime(timezone=True), server_default=func.now())

    shop    = relationship("ShopifyUser",    back_populates="productEmbeddings")
    product = relationship("ScrapedProduct", back_populates="embeddings")
    variant = relationship("ScrapedVariant", back_populates="embeddings")


# ─────────────────────────────────────────────────────────────────────────────
# Error log — permanent record of failed extraction/semantic tasks
# ─────────────────────────────────────────────────────────────────────────────

class ScrapingError(Base):
    __tablename__ = "ScrapingError"

    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain  = Column("shopDomain",  String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    configId    = Column("configId",    String, ForeignKey("ScrapingConfig.id"), nullable=False)
    productUrl  = Column("productUrl",  String, nullable=False)
    gcsRef      = Column("gcsRef",      String)
    errorType   = Column("errorType",   String, nullable=False)
    errorDetail = Column("errorDetail", String)
    taskName    = Column("taskName",    String, nullable=False)
    failedAt    = Column("failedAt",    DateTime(timezone=True), server_default=func.now())

    shop   = relationship("ShopifyUser",    back_populates="scrapingErrors")
    config = relationship("ScrapingConfig", back_populates="errors")
