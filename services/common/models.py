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
from sqlalchemy.dialects.postgresql import ARRAY, ENUM as PgEnum, JSONB
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

_candidate_status = PgEnum(
    "PENDING", "VERIFIED", "REJECTED", "DEAD",
    name="CandidateStatus",
    create_type=False,
)

_discovery_status = PgEnum(
    "QUEUED", "RUNNING", "COMPLETED", "FAILED",
    name="DiscoveryStatus",
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
    categoryTop   = Column("categoryTop",   String)
    productGender = Column("productGender", String)
    searchQuery               = Column("searchQuery",               String,  nullable=True)
    searchQueryOverride       = Column("searchQueryOverride",       String,  nullable=True)
    dynamicPricingEnabled     = Column("dynamicPricingEnabled",     Boolean, nullable=False, default=False)
    floorPrice                = Column("floorPrice",                Numeric(10, 2))
    ceilingPrice              = Column("ceilingPrice",              Numeric(10, 2))
    frequencyInterval         = Column("frequencyInterval",         Integer)
    frequencyUnit             = Column("frequencyUnit",             String)
    lastDiscoveryAt           = Column("lastDiscoveryAt",           DateTime(timezone=True), nullable=True)
    discoveryNumResults       = Column("discoveryNumResults",       Integer, nullable=False, default=10)
    createdAt   = Column("createdAt",   DateTime(timezone=True), server_default=func.now())
    updatedAt   = Column("updatedAt",   DateTime(timezone=True), default=func.now(), onupdate=func.now())

    variants             = relationship("ShopifyVariant",       back_populates="product", cascade="all, delete-orphan")
    competitorCandidates = relationship("CompetitorCandidate",  back_populates="shopifyProduct", cascade="all, delete-orphan")
    discoveryJobs        = relationship("DiscoveryJob",         back_populates="shopifyProduct", cascade="all, delete-orphan")


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
    matchedAt  = Column("matchedAt",  DateTime(timezone=True), nullable=True)

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
    categoryTop    = Column("categoryTop",    String)
    productGender  = Column("productGender",  String)
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
    currency      = Column("currency",     String(3), nullable=False, default="INR")
    isInStock     = Column("isInStock",    Boolean, default=True)
    stockQuantity = Column("stockQuantity",Integer)
    semanticText  = Column("semanticText", Text)
    createdAt     = Column("createdAt",    DateTime(timezone=True), server_default=func.now())
    updatedAt     = Column("updatedAt",    DateTime(timezone=True), default=func.now(), onupdate=func.now())

    product        = relationship("ScrapedProduct",  back_populates="variants")
    embeddings     = relationship("ProductEmbedding", back_populates="variant", cascade="all, delete-orphan")
    productMatches = relationship("ProductMatch",     back_populates="competitorVariant")
    observations   = relationship("CompetitorPriceObservation", back_populates="competitorVariant", cascade="all, delete-orphan")


class CompetitorPriceObservation(Base):
    """Append-only price observation for a competitor variant.

    TimescaleDB hypertable on `observedAt`. The primary key is
    `(id, observedAt)` — Timescale requires the partitioning column to
    participate in any unique constraint.
    """
    __tablename__ = "CompetitorPriceObservation"

    id                  = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain          = Column("shopDomain", String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    competitorVariantId = Column("competitorVariantId", String, ForeignKey("ScrapedVariant.id", ondelete="CASCADE"), nullable=False)
    price               = Column("price", Numeric(10, 2), nullable=False)
    currency            = Column("currency", String(3), nullable=False, default="INR")
    isInStock           = Column("isInStock", Boolean, default=True)
    observedAt          = Column("observedAt", DateTime(timezone=True), primary_key=True, server_default=func.now())

    competitorVariant   = relationship("ScrapedVariant", back_populates="observations")


# ─────────────────────────────────────────────────────────────────────────────
# URL lifecycle tracking
# ─────────────────────────────────────────────────────────────────────────────

class ProductUrl(Base):
    __tablename__ = "ProductUrl"

    id                = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain        = Column("shopDomain",        String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    shopifyProductId  = Column("shopifyProductId",  String, ForeignKey("ShopifyProduct.id", ondelete="CASCADE"), nullable=True)
    configId          = Column("configId",          String, ForeignKey("ScrapingConfig.id"), nullable=True)
    prodId            = Column("prodId",            String, ForeignKey("ScrapedProduct.id", ondelete="CASCADE"), nullable=False)
    url               = Column("url",               String, unique=True, nullable=False)
    status            = Column("status",            _url_status, nullable=False, default="ACTIVE")
    failCount         = Column("failCount",         Integer, default=0)
    frequencyInterval = Column("frequencyInterval", Integer)
    frequencyUnit     = Column("frequencyUnit",     String, default="daily")
    lastScrapedAt     = Column("lastScrapedAt",     DateTime(timezone=True))
    nextRunAt         = Column("nextRunAt",         DateTime(timezone=True))
    createdAt         = Column("createdAt",         DateTime(timezone=True), server_default=func.now())

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
    matchedAt    = Column("matchedAt",    DateTime(timezone=True), nullable=True)

    shop    = relationship("ShopifyUser",    back_populates="productEmbeddings")
    product = relationship("ScrapedProduct", back_populates="embeddings")
    variant = relationship("ScrapedVariant", back_populates="embeddings")


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


# ─────────────────────────────────────────────────────────────────────────────
# Competitor discovery (product-rooted flow)
# ─────────────────────────────────────────────────────────────────────────────

class DiscoveryJob(Base):
    __tablename__ = "DiscoveryJob"

    id               = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain       = Column("shopDomain",       String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    shopifyProductId = Column("shopifyProductId", String, ForeignKey("ShopifyProduct.id", ondelete="CASCADE"), nullable=False)
    status           = Column("status",           _discovery_status, nullable=False, default="QUEUED")
    query            = Column("query",            Text)
    numResults       = Column("numResults",       Integer, nullable=False, default=10)
    candidateCount   = Column("candidateCount",   Integer, nullable=False, default=0)
    error            = Column("error",            Text)
    requestedAt      = Column("requestedAt",      DateTime(timezone=True), server_default=func.now())
    completedAt      = Column("completedAt",      DateTime(timezone=True))

    shopifyProduct = relationship("ShopifyProduct", back_populates="discoveryJobs")
    candidates     = relationship("CompetitorCandidate", back_populates="discoveryJob")


class CompetitorCandidate(Base):
    __tablename__ = "CompetitorCandidate"
    __table_args__ = (
        UniqueConstraint("shopifyProductId", "url", name="CompetitorCandidate_shopifyProductId_url_key"),
    )

    id               = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain       = Column("shopDomain",       String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    shopifyProductId = Column("shopifyProductId", String, ForeignKey("ShopifyProduct.id", ondelete="CASCADE"), nullable=False)
    discoveryJobId   = Column("discoveryJobId",   String, ForeignKey("DiscoveryJob.id", ondelete="SET NULL"), nullable=True)

    url    = Column("url",    String, nullable=False)
    domain = Column("domain", String, nullable=False)
    source = Column("source", String, nullable=False)  # serper_search | serper_shopping | manual

    serpTitle    = Column("serpTitle",    String)
    serpSnippet  = Column("serpSnippet",  Text)
    serpPrice    = Column("serpPrice",    Numeric(10, 2))
    embedScore   = Column("embedScore",   Numeric(5, 4))
    rerankScore  = Column("rerankScore",  Numeric(4, 3))
    rerankReason = Column("rerankReason", Text)

    status       = Column("status",       _candidate_status, nullable=False, default="PENDING")
    rejectReason = Column("rejectReason", String)

    scrapedProductId = Column("scrapedProductId", String, ForeignKey("ScrapedProduct.id", ondelete="SET NULL"), nullable=True)

    discoveredAt = Column("discoveredAt", DateTime(timezone=True), server_default=func.now())
    scrapedAt    = Column("scrapedAt",    DateTime(timezone=True))
    verifiedAt   = Column("verifiedAt",   DateTime(timezone=True))

    shopifyProduct = relationship("ShopifyProduct", back_populates="competitorCandidates")
    discoveryJob   = relationship("DiscoveryJob",   back_populates="candidates")


class ShopSettings(Base):
    __tablename__ = "ShopSettings"

    shopDomain               = Column("shopDomain", String, ForeignKey("ShopifyUser.shopDomain"), primary_key=True)
    markupPct                = Column("markupPct",  Numeric(5, 4), nullable=False, default=0.02)
    minCompetitorsRequired   = Column("minCompetitorsRequired",   Integer, nullable=False, default=2)
    maxCompetitorsPerProduct = Column("maxCompetitorsPerProduct", Integer, nullable=False, default=8)
    frequencyInterval        = Column("frequencyInterval", Integer, nullable=False, default=1)
    frequencyUnit            = Column("frequencyUnit",     String,  nullable=False, default="daily")
    marketplaceBlocklist     = Column("marketplaceBlocklist", ARRAY(String), nullable=False, default=list)
    killSwitch               = Column("killSwitch", Boolean, nullable=False, default=False)
    updatedAt                = Column("updatedAt", DateTime(timezone=True), default=func.now(), onupdate=func.now())
