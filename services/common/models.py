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

from sqlalchemy import BIGINT, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, func, UniqueConstraint
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
    "PENDING", "SCRAPED", "VERIFIED", "REJECTED", "DEAD",
    name="CandidateStatus",
    create_type=False,
)

_discovery_status = PgEnum(
    "QUEUED", "RUNNING", "COMPLETED", "FAILED",
    name="DiscoveryStatus",
    create_type=False,
)

_match_confidence_tier = PgEnum(
    "CONFIRMED", "LIKELY", "WEAK",
    name="MatchConfidenceTier",
    create_type=False,
)

_pricing_tier = PgEnum(
    "BUDGET", "COMPETITIVE", "PREMIUM",
    name="PricingTier",
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
    productSyncState     = Column("productSyncState",     String, nullable=False, server_default="'IDLE'")
    productSyncStartedAt = Column("productSyncStartedAt", DateTime(timezone=True), nullable=True)
    productSyncedAt      = Column("productSyncedAt",      DateTime(timezone=True), nullable=True)
    productSyncError     = Column("productSyncError",     String, nullable=True)

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
    semanticStatus        = Column("semanticStatus",        String, nullable=False, server_default="'PENDING'")
    semanticClaimedAt     = Column("semanticClaimedAt",     DateTime(timezone=True), nullable=True)
    semanticVersion       = Column("semanticVersion",       Integer, nullable=False, server_default="0")
    semanticAttempts      = Column("semanticAttempts",      Integer, nullable=False, server_default="0")
    semanticFailureReason = Column("semanticFailureReason", String, nullable=True)
    categoryTop   = Column("categoryTop",   String)
    productGender = Column("productGender", String)
    searchQuery               = Column("searchQuery",               String,  nullable=True)
    searchQueryOverride       = Column("searchQueryOverride",       String,  nullable=True)
    dynamicPricingEnabled     = Column("dynamicPricingEnabled",     Boolean, nullable=False, default=False)
    dynamicPricingConfiguredAt = Column("dynamicPricingConfiguredAt", DateTime(timezone=True), nullable=True)
    avgBasePrice              = Column("avgBasePrice",              Numeric(10, 2))
    frequencyInterval         = Column("frequencyInterval",         Integer)
    frequencyUnit             = Column("frequencyUnit",             String)
    lastDiscoveryAt           = Column("lastDiscoveryAt",           DateTime(timezone=True), nullable=True)
    discoveryNumResults       = Column("discoveryNumResults",       Integer, nullable=False, default=10)
    listingExpansionCap       = Column("listingExpansionCap",       Integer, nullable=True)
    pricingTier         = Column("pricingTier",         _pricing_tier, nullable=False, server_default="'COMPETITIVE'")
    minPriceOverride    = Column("minPriceOverride",    Numeric(10, 2), nullable=True)
    maxPriceOverride    = Column("maxPriceOverride",    Numeric(10, 2), nullable=True)
    maxAutoApplyChangePctOverride = Column("maxAutoApplyChangePctOverride", Float, nullable=True)
    lifetimeCapPctOverride        = Column("lifetimeCapPctOverride", Float, nullable=True)
    syncPrice           = Column("syncPrice",           Boolean, nullable=False, default=True)
    syncedAt            = Column("syncedAt",            DateTime(timezone=True), nullable=True)
    lastDecisionAt      = Column("lastDecisionAt",      DateTime(timezone=True), nullable=True)
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
    basePrice      = Column("basePrice",      Numeric(10, 2))
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


class VariantCompetitorStats(Base):
    __tablename__ = "VariantCompetitorStats"

    shopifyVariantId = Column("shopifyVariantId", String, ForeignKey("ShopifyVariant.id", ondelete="CASCADE"), primary_key=True)
    shopDomain       = Column("shopDomain",       String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    competitorCount  = Column("competitorCount",  Integer, nullable=False, default=0)
    minPrice         = Column("minPrice",         Numeric(10, 2), nullable=True)
    median           = Column("median",           Numeric(10, 2), nullable=True)
    maxPrice         = Column("maxPrice",         Numeric(10, 2), nullable=True)
    lastUpdatedAt    = Column("lastUpdatedAt",    DateTime(timezone=True), server_default=func.now())


class PriceDecision(Base):
    __tablename__ = "PriceDecision"

    id               = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain       = Column("shopDomain",       String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    shopifyVariantId = Column("shopifyVariantId", String, ForeignKey("ShopifyVariant.id", ondelete="CASCADE"), nullable=False)
    oldPrice         = Column("oldPrice",         Numeric(10, 2), nullable=False)
    newPrice         = Column("newPrice",         Numeric(10, 2), nullable=False)
    reason           = Column("reason",           String, nullable=False)
    decidedAt        = Column("decidedAt",        DateTime(timezone=True), server_default=func.now())
    appliedAt        = Column("appliedAt",        DateTime(timezone=True), nullable=True)
    changePct        = Column("changePct",        Float, nullable=True)
    tierAtDecision   = Column("tierAtDecision",   _pricing_tier, nullable=True)
    autoApplied      = Column("autoApplied",      Boolean, nullable=False, default=False)
    revertedAt       = Column("revertedAt",       DateTime(timezone=True), nullable=True)


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
    url               = Column("url",               String, nullable=False)
    status            = Column("status",            _url_status, nullable=False, default="ACTIVE")
    failCount         = Column("failCount",         Integer, default=0)
    lastScrapedAt     = Column("lastScrapedAt",     DateTime(timezone=True))
    nextRunAt         = Column("nextRunAt",         DateTime(timezone=True))
    createdAt         = Column("createdAt",         DateTime(timezone=True), server_default=func.now())

    shop    = relationship("ShopifyUser",    back_populates="productUrls")
    config  = relationship("ScrapingConfig", back_populates="product_urls")
    product = relationship("ScrapedProduct", back_populates="urls")

    __table_args__ = (
        UniqueConstraint("shopifyProductId", "url", name="ProductUrl_shopifyProductId_url_key"),
    )


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


class ProductLevelMatch(Base):
    __tablename__ = "ProductLevelMatch"
    __table_args__ = (
        UniqueConstraint(
            "shopifyProductId", "scrapedProductId",
            name="ProductLevelMatch_shopifyProductId_scrapedProductId_key",
        ),
    )

    id               = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    shopDomain       = Column("shopDomain",       String, ForeignKey("ShopifyUser.shopDomain"), nullable=False)
    shopifyProductId = Column("shopifyProductId", String, ForeignKey("ShopifyProduct.id", ondelete="CASCADE"), nullable=False)
    scrapedProductId = Column("scrapedProductId", String, ForeignKey("ScrapedProduct.id", ondelete="CASCADE"), nullable=False)
    confidence       = Column("confidence",       Numeric(4, 3), nullable=False, default=0)
    confidenceTier   = Column("confidenceTier",   _match_confidence_tier, nullable=False)
    confirmedByMerchant = Column("confirmedByMerchant", Boolean, nullable=False, default=False)
    rejectedByMerchant = Column("rejectedByMerchant", Boolean, nullable=False, default=False)
    reviewedAt       = Column("reviewedAt",       DateTime(timezone=True), nullable=True)
    updatedAt        = Column("updatedAt",        DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())


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
    maxCompetitorsPerProduct = Column("maxCompetitorsPerProduct", Integer, nullable=False, default=8)
    frequencyInterval        = Column("frequencyInterval", Integer, nullable=False, default=1)
    frequencyUnit            = Column("frequencyUnit",     String,  nullable=False, default="daily")
    defaultPricingTier       = Column("defaultPricingTier", _pricing_tier, nullable=False, server_default="'COMPETITIVE'")
    listingExpansionCap      = Column("listingExpansionCap", Integer, nullable=True)
    marketplaceBlocklist     = Column("marketplaceBlocklist", ARRAY(String), nullable=False, default=list)
    autoRescrapeEnabled      = Column("autoRescrapeEnabled", Boolean, nullable=False, default=True)
    serperGl                 = Column("serperGl",       String, nullable=False, default="in")
    serperHl                 = Column("serperHl",       String, nullable=False, default="en")
    serperLocation           = Column("serperLocation", String, nullable=False, default="Kochi, Kerala")
    updatedAt                = Column("updatedAt", DateTime(timezone=True), default=func.now(), onupdate=func.now())
    currency                 = Column("currency", String, nullable=True)
    minCompetitorsToPrice    = Column("minCompetitorsToPrice", Integer, nullable=False, default=4)
    topKCompetitors          = Column("topKCompetitors", Integer, nullable=False, default=4)
    maxAutoApplyChangePct    = Column("maxAutoApplyChangePct", Float, nullable=False, default=0.05)
    lifetimeCapPct           = Column("lifetimeCapPct", Float, nullable=False, default=0.25)
    budgetUndercut           = Column("budgetUndercut", Float, nullable=False, default=0.05)
    premiumUplift            = Column("premiumUplift", Float, nullable=False, default=0.05)
    includeOosInPricing      = Column("includeOosInPricing", Boolean, nullable=False, default=False)
    discoveryNumResults      = Column("discoveryNumResults", Integer, nullable=False, default=10)
    minChangePctThreshold    = Column("minChangePctThreshold", Float, nullable=False, default=0.005)
    minFreshnessHours        = Column("minFreshnessHours", Integer, nullable=False, default=24)


# ─────────────────────────────────────────────────────────────────────────────
# Chatbot (merchant-facing assistant)
# ─────────────────────────────────────────────────────────────────────────────

_chat_role = PgEnum(
    "user", "assistant", "tool",
    name="ChatRole",
    create_type=False,
)

_preview_kind = PgEnum(
    "price_change", "dynamic_pricing_toggle",
    name="PreviewKind",
    create_type=False,
)

# Public names for import by other modules
ChatRole = _chat_role
PreviewKind = _preview_kind


class ChatSession(Base):
    __tablename__ = "ChatSession"
    __table_args__ = (
        Index("ChatSession_shopDomain_updatedAt_idx", "shopDomain", "updatedAt"),
    )

    id         = Column(String, primary_key=True)
    shopDomain = Column("shopDomain", String, ForeignKey("ShopifyUser.shopDomain", ondelete="RESTRICT", onupdate="CASCADE"), nullable=False)
    userId     = Column("userId",     String, nullable=True)
    title      = Column("title",      String, nullable=True)
    runningSummary = Column("runningSummary", Text, nullable=True)
    resolvedProductIds = Column("resolvedProductIds", JSONB, nullable=False, server_default="'[]'")
    createdAt  = Column("createdAt",  DateTime(timezone=True), server_default=func.now(), nullable=False)
    updatedAt  = Column("updatedAt",  DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")
    previews = relationship("ChatPreview", back_populates="session", cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "ChatMessage"
    __table_args__ = (
        Index("ChatMessage_sessionId_createdAt_idx", "sessionId", "createdAt"),
        Index("ChatMessage_sessionId_pinned_idx", "sessionId", "pinned"),
    )

    id         = Column(String, primary_key=True)
    sessionId  = Column("sessionId", String, ForeignKey("ChatSession.id", ondelete="CASCADE"), nullable=False)
    role       = Column("role",      _chat_role, nullable=False)
    content    = Column("content",   JSONB, nullable=False)
    tokenCount = Column("tokenCount", Integer, nullable=True)
    pinned     = Column("pinned",    Boolean, nullable=False, server_default="false")
    createdAt  = Column("createdAt", DateTime(timezone=True), server_default=func.now(), nullable=False)

    session = relationship("ChatSession", back_populates="messages")


class ChatPreview(Base):
    __tablename__ = "ChatPreview"
    __table_args__ = (
        Index("ChatPreview_shopDomain_expiresAt_idx", "shopDomain", "expiresAt"),
    )

    id          = Column(String, primary_key=True)
    sessionId   = Column("sessionId",   String, ForeignKey("ChatSession.id", ondelete="CASCADE"), nullable=False)
    shopDomain  = Column("shopDomain",  String, nullable=False)
    kind        = Column("kind",        _preview_kind, nullable=False)
    scopeFilter = Column("scopeFilter", JSONB, nullable=False)
    change      = Column("change",      JSONB, nullable=False)
    variantIds  = Column("variantIds",  ARRAY(String), nullable=False)
    summary     = Column("summary",     JSONB, nullable=False)
    expiresAt   = Column("expiresAt",   DateTime(timezone=True), nullable=False)
    appliedAt   = Column("appliedAt",   DateTime(timezone=True), nullable=True)
    appliedBy   = Column("appliedBy",   String, nullable=True)
    result      = Column("result",      JSONB, nullable=True)
    createdAt   = Column("createdAt",   DateTime(timezone=True), server_default=func.now(), nullable=False)

    session = relationship("ChatSession", back_populates="previews")


# ─────────────────────────────────────────────────────────────────────────────
# Activity event log for pipeline visibility
