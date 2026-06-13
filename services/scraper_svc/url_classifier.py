"""
services/scraper_svc/url_classifier.py

Cheap, deterministic URL classifier used by scrape_candidate to decide whether
a Firecrawl'd URL is a listing/search page (fan out into per-card scrapes) or a
real product page (run the normal Groq extract path).

A miss in either direction is recoverable: a misclassified product page yields
zero same-domain card URLs and the parent ends as "listing_expanded:0_cards";
a misclassified listing falls through to the regular extractor which will
typically produce a low-quality ScrapedProduct that the matcher discards.
"""
from __future__ import annotations

from urllib.parse import urlparse, parse_qs

# Path tokens that strongly indicate a listing/search/category page across
# major storefronts (Myntra, Amazon, Flipkart, generic Shopify, etc.).
_LISTING_PATH_TOKENS: tuple[str, ...] = (
    "/search",
    "/category",
    "/categories",
    "/c/",
    "/c-",
    "/collection",
    "/collections",
    "/shop",
    "/listing",
    "/browse",
)

# `k` is Amazon's search-query key; the others cover Flipkart, generic stores.
_LISTING_QUERY_KEYS: tuple[str, ...] = ("q", "k", "search", "query", "category")


def is_listing_url(url: str) -> bool:
    """Return True if `url` looks like a search/category/listing page.

    Three signals are OR'd:
      1. Path contains a listing-shaped token (e.g. /search, /collection).
      2. Query string carries a search/category key (e.g. ?q=, ?k=).
      3. Single-segment hyphenated slug pattern (e.g. /nike-women-handbags).
         Real product URLs on Myntra/Amazon/etc. always have multiple
         segments and/or a numeric ID, so a lone dashed slug almost always
         means a brand/category listing page.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    path_lower = (parsed.path or "").lower()

    # Shopify product-page convention: `/collections/<x>/products/<y>` and
    # generic `/products/<slug>` paths are single-product URLs even when the
    # path contains the `/collections` listing token. Exempt them first so the
    # token scan below doesn't misfire.
    segments_raw = [s for s in path_lower.strip("/").split("/") if s]
    if "products" in segments_raw:
        idx = segments_raw.index("products")
        if idx + 1 < len(segments_raw) and segments_raw[idx + 1]:
            return False

    if any(tok in path_lower for tok in _LISTING_PATH_TOKENS):
        return True

    if parsed.query:
        qs = parse_qs(parsed.query)
        if any(k in qs for k in _LISTING_QUERY_KEYS):
            return True

    segments = [s for s in path_lower.strip("/").split("/") if s]
    if len(segments) == 1 and "-" in segments[0] and not any(ch.isdigit() for ch in segments[0]):
        return True

    return False


def registrable_domain(url_or_host: str) -> str:
    """Fold subdomains so `www.myntra.com` and `m.myntra.com` compare equal.

    Cheap heuristic: take the last two labels of the hostname. This is wrong
    for ccTLDs like `co.uk` (would collapse to `co.uk` and merge all sites),
    but every storefront we currently scrape lives on a single-label TLD
    (.com, .in, .io, .net). Worth swapping for `tldextract` if we ever hit
    a .co.uk listing.
    """
    try:
        host = urlparse(url_or_host).netloc or url_or_host
    except Exception:
        host = url_or_host
    host = host.lower().split(":")[0]  # strip port
    labels = host.split(".")
    if len(labels) < 2:
        return host
    return ".".join(labels[-2:])
