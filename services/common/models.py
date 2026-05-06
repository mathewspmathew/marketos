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

from sqlalchemy import BIGINT, Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, Text, func, UniqueConstraint
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
    productMatches    = relationship("ProductMatch",      back_populates="shop")


# ─────────────────────────────────────────────────────────────────────────────
# Internal Shopify store data (read-only from Python side; Shopify sync writes)
# ─────────────────────────────────────────────────────────────────────────────

class ShopifyProduct(Base):
    __tablename__ = "ShopifyProduct"

    id          = Column(String, primary_key=True)
    shopDomain  = Column("shopDomain",  String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    title       = Column("title",       String, nullable=False)
    description = Column("description", Text, default="")
    vendor      = Column("vendor",      String)
    productType = Column("productType", String, default="")
    tags        = Column("tags",        JSONB, default=list)
    imageUrl    = Column("imageUrl",    String)
    handle      = Column("handle",      String)
    status      = Column("status",      String, default="ACTIVE")
    createdAt   = Column("createdAt",   DateTime(timezone=True), server_default=func.now())
    updatedAt   = Column("updatedAt",   DateTime(timezone=True), default=func.now(), onupdate=func.now())

    variants = relationship("ShopifyVariant", back_populates="product", cascade="all, delete-orphan")


class ShopifyVariant(Base):
    __tablename__ = "ShopifyVariant"

    id             = Column(String, primary_key=True)
    productId      = Column("productId",      String, ForeignKey("ShopifyProduct.id", ondelete="CASCADE"), nullable=False)
    sku            = Column("sku",            String)
    barcode        = Column("barcode",        String)
    title          = Column("title",          String, nullable=False, default="Default Title")
    options        = Column("options",        JSONB)
    imageUrl       = Column("imageUrl",       String)
    currentPrice   = Column("currentPrice",   Numeric(10, 2), nullable=False)
    compareAtPrice = Column("compareAtPrice", Numeric(10, 2))
    isInStock      = Column("isInStock",      Boolean, default=True)
    stockQuantity  = Column("stockQuantity",  Integer)
    semanticText   = Column("semanticText",   Text)
    updatedAt      = Column("updatedAt",      DateTime(timezone=True), default=func.now(), onupdate=func.now())

    product        = relationship("ShopifyProduct",  back_populates="variants")
    embedding      = relationship("ShopifyEmbedding", back_populates="variant", uselist=False, cascade="all, delete-orphan")
    productMatches = relationship("ProductMatch",     back_populates="shopifyVariant", cascade="all, delete-orphan")


class ShopifyEmbedding(Base):
    __tablename__ = "ShopifyEmbedding"
    # vector columns (vectorText, vectorImg) omitted — use raw SQL for pgvector writes

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    variantId  = Column("variantId",  String, ForeignKey("ShopifyVariant.id", ondelete="CASCADE"), nullable=False, unique=True)
    shopDomain = Column("shopDomain", String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    embeddedAt = Column("embeddedAt", DateTime(timezone=True), server_default=func.now())
    updatedAt  = Column("updatedAt",  DateTime(timezone=True), default=func.now(), onupdate=func.now())

    variant = relationship("ShopifyVariant", back_populates="embedding")


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
    frequencyUnit     = Column("frequencyUnit",     String, default="nofreq")
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

    shop           = relationship("ShopifyUser",    back_populates="scrapedProducts")
    variants       = relationship("ScrapedVariant", back_populates="product", cascade="all, delete-orphan")
    urls           = relationship("ProductUrl",      back_populates="product", cascade="all, delete-orphan")
    embeddings     = relationship("ProductEmbedding", back_populates="product", cascade="all, delete-orphan")
    productMatches = relationship("ProductMatch",    back_populates="competitorProduct", cascade="all, delete-orphan")


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

    product        = relationship("ScrapedProduct",  back_populates="variants")
    embeddings     = relationship("ProductEmbedding", back_populates="variant", cascade="all, delete-orphan")
    productMatches = relationship("ProductMatch",     back_populates="competitorVariant")


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
    vectorizedAt = Column("vectorizedAt", DateTime(timezone=True), server_default=func.now())

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


# ─────────────────────────────────────────────────────────────────────────────
# Similarity matches: merchant variant ↔ competitor variant (one row per pair)
# ─────────────────────────────────────────────────────────────────────────────

class ProductMatch(Base):
    __tablename__ = "ProductMatch"
    __table_args__ = (
        UniqueConstraint("shopifyVariantId", "competitorVariantId", name="ProductMatch_shopifyVariantId_competitorVariantId_key"),
    )

    id                  = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain          = Column("shopDomain",          String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    shopifyVariantId    = Column("shopifyVariantId",    String, ForeignKey("ShopifyVariant.id", ondelete="CASCADE"), nullable=False)
    competitorVariantId = Column("competitorVariantId", String, ForeignKey("ScrapedVariant.id",  ondelete="SET NULL"), nullable=True)
    competitorProdId    = Column("competitorProdId",    String, ForeignKey("ScrapedProduct.id",  ondelete="CASCADE"), nullable=False)

    matchScore     = Column("matchScore",     Numeric(5, 2),  nullable=False)
    matchType      = Column("matchType",      String,         nullable=False, default="semantic")
    vectorDistance = Column("vectorDistance", Numeric(10, 6), nullable=False)
    thresholdUsed  = Column("thresholdUsed",  Numeric(5, 4),  nullable=False)

    matchedAt = Column("matchedAt", DateTime(timezone=True), server_default=func.now())
    updatedAt = Column("updatedAt", DateTime(timezone=True), default=func.now(), onupdate=func.now())

    shop              = relationship("ShopifyUser",    back_populates="productMatches")
    shopifyVariant    = relationship("ShopifyVariant", back_populates="productMatches")
    competitorVariant = relationship("ScrapedVariant", back_populates="productMatches")
    competitorProduct = relationship("ScrapedProduct", back_populates="productMatches")
