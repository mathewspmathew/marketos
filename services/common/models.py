"""
services/common/models.py

SQLAlchemy ORM models for the tables Python services read/write.
Table and column names match exactly what Prisma created — SQLAlchemy
never runs migrations; that stays with Prisma JS in shopify_ui/.

Note on updatedAt: Prisma's @updatedAt does NOT create a DB-level DEFAULT —
it sets the value at the application (Prisma JS client) level. We use
`default=func.now()` so SQLAlchemy supplies `NOW()` in every INSERT, and
the DB also has a DEFAULT NOW() added via migration so raw INSERTs are safe.
"""
import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM as PgEnum, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# Reference the existing PG enum that Prisma created — do not re-create it.
_scrape_status = PgEnum(
    "IDLE", "QUEUED", "RUNNING", "SCRAPED_FIRST",
    name="ScrapeStatus",
    create_type=False,
)


class ScrapingConfig(Base):
    __tablename__ = "ScrapingConfig"

    id                = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    userId            = Column("userId",            String,  nullable=False)
    shopId            = Column("shopId",            String)
    competitorUrl     = Column("competitorUrl",     String,  nullable=False)
    includeImages     = Column("includeImages",     Boolean, default=True)
    productLimit      = Column("productLimit",      Integer)
    frequencyInterval = Column("frequencyInterval", Integer)
    nextRunAt         = Column("nextRunAt",         DateTime(timezone=True))
    isActive          = Column("isActive",          Boolean, default=True)
    status            = Column("status",            _scrape_status, nullable=False, default="IDLE")
    createdAt         = Column("createdAt",         DateTime(timezone=True), server_default=func.now())
    updatedAt         = Column("updatedAt",         DateTime(timezone=True), default=func.now(), onupdate=func.now())


class ScrapedProduct(Base):
    __tablename__ = "ScrapedProduct"

    id             = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    userId         = Column("userId",         String, nullable=False)
    url            = Column("url",            String, unique=True, nullable=False)
    domain         = Column("domain",         String, nullable=False)
    title          = Column("title",          String, nullable=False)
    description    = Column("description",    Text)
    vendor         = Column("vendor",         String)
    productType    = Column("productType",    String)
    tags           = Column("tags",           JSONB,  default=list)
    imageUrl       = Column("imageUrl",       String)
    specifications = Column("specifications", JSONB)
    semanticText   = Column("semanticText",   Text)
    vectorized     = Column("vectorized",     Boolean, default=False)
    scrapedAt      = Column("scrapedAt",      DateTime(timezone=True), server_default=func.now())
    updatedAt      = Column("updatedAt",      DateTime(timezone=True), default=func.now(), onupdate=func.now())

    variants = relationship("ScrapedVariant", back_populates="product", cascade="all, delete-orphan")


class ScrapedVariant(Base):
    __tablename__ = "ScrapedVariant"

    id                  = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    productId           = Column("productId", String, ForeignKey("ScrapedProduct.id", ondelete="CASCADE"), nullable=False)
    userId              = Column("userId",    String, nullable=False)
    sku                 = Column("sku",       String)
    barcode             = Column("barcode",   String)
    title               = Column("title",     String, nullable=False, default="Default Title")
    options             = Column("options",   JSONB)
    currentPrice        = Column("currentPrice",  Numeric(10, 2), nullable=False)
    originalPrice       = Column("originalPrice", Numeric(10, 2))
    isInStock           = Column("isInStock",     Boolean, default=True)
    stockQuantity       = Column("stockQuantity", Integer)
    variantSemanticText = Column("variantSemanticText", Text)
    vectorized          = Column("vectorized", Boolean, default=False)
    scrapedAt           = Column("scrapedAt", DateTime(timezone=True), server_default=func.now())
    updatedAt           = Column("updatedAt", DateTime(timezone=True), default=func.now(), onupdate=func.now())

    product = relationship("ScrapedProduct", back_populates="variants")
