"""Render the MarketOS end-to-end data flow as a PNG diagram."""
from __future__ import annotations

from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent.parent / "docs" / "marketos_flow.png"
OUT.parent.mkdir(parents=True, exist_ok=True)

# Color palette
C_USER   = "#FFE4B5"
C_UI     = "#A7D8FF"
C_QUEUE  = "#FFD1DC"
C_WORKER = "#C5E1A5"
C_STORE  = "#D7BDE2"
C_EXT    = "#F5B7B1"
C_BEAT   = "#FAD7A0"

fig, ax = plt.subplots(figsize=(20, 13))
ax.set_xlim(0, 20)
ax.set_ylim(0, 13)
ax.axis("off")
ax.set_title("MarketOS — End-to-End Data Flow",
             fontsize=20, fontweight="bold", pad=14)


def box(x, y, w, h, label, color, fontsize=9, bold=True):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle="round,pad=0.04,rounding_size=0.15",
                       linewidth=1.4, edgecolor="#333", facecolor=color)
    ax.add_patch(p)
    ax.text(x + w / 2, y + h / 2, label,
            ha="center", va="center", fontsize=fontsize,
            fontweight="bold" if bold else "normal", wrap=True)


def arrow(x1, y1, x2, y2, label="", color="#222", curve=0.0,
          ls="-", lw=1.6, label_offset=(0, 0.18), fs=8):
    a = FancyArrowPatch((x1, y1), (x2, y2),
                        arrowstyle="-|>", mutation_scale=14,
                        linewidth=lw, color=color, linestyle=ls,
                        connectionstyle=f"arc3,rad={curve}")
    ax.add_patch(a)
    if label:
        mx, my = (x1 + x2) / 2 + label_offset[0], (y1 + y2) / 2 + label_offset[1]
        ax.text(mx, my, label, ha="center", va="center", fontsize=fs,
                style="italic", color="#222",
                bbox=dict(facecolor="white", edgecolor="none",
                          alpha=0.85, pad=1))


# ── Row 1 (top): Merchant + UI + Stores ────────────────────────────────────
box(0.3, 11.2, 2.6, 1.3,
    "👤 Merchant\n(Shopify Store Owner)", C_USER, 10)
box(3.6, 11.2, 3.6, 1.3,
    "🛍️  Shopify Admin\n(Embedded App · React Router 7)\nshopify_ui/", C_UI, 9)
box(8.0, 11.2, 3.6, 1.3,
    "⚡ API Gateway\n(FastAPI · :8000)\nservices/api_gateway", C_UI, 9)
box(12.4, 11.2, 3.4, 1.3,
    "🐘 PostgreSQL + pgvector\n(Prisma JS + Python)\nshared DB", C_STORE, 9)
box(16.2, 11.2, 3.5, 1.3,
    "☁️  Google Cloud Storage\nmarkdown · images buckets", C_STORE, 9)

# ── Row 2: Scheduler trigger ───────────────────────────────────────────────
box(0.3, 8.8, 2.6, 1.2,
    "⏰ Celery Beat\n(every 30s)", C_BEAT, 10)
box(3.6, 8.8, 2.4, 1.2,
    "📬 scheduler_queue", C_QUEUE, 10)
box(6.6, 8.8, 3.0, 1.2,
    "🧭 scheduler-worker\ncheck_idle_configs", C_WORKER, 10)

# ── Row 3: Scraping ────────────────────────────────────────────────────────
box(0.3, 6.6, 2.6, 1.2,
    "📬 scraping_queue", C_QUEUE, 10)
box(3.6, 6.6, 3.0, 1.2,
    "🕷️  scraper-worker\n(Firecrawl API)", C_WORKER, 10)
box(7.4, 6.6, 2.6, 1.2,
    "📬 extraction_queue", C_QUEUE, 10)
box(10.7, 6.6, 3.0, 1.2,
    "🤖 extraction-worker\n(Groq LLM · llama-3.1)", C_WORKER, 10)
box(14.5, 6.6, 2.5, 1.2,
    "🔥 Firecrawl\n(external)", C_EXT, 9)
box(17.2, 6.6, 2.5, 1.2,
    "⚡ Groq API\n(external)", C_EXT, 9)

# ── Row 4: Embedding + Semantic ────────────────────────────────────────────
box(0.3, 4.4, 2.6, 1.2,
    "📬 embedding_queue", C_QUEUE, 10)
box(3.6, 4.4, 3.0, 1.2,
    "🧮 embedding-worker\n(Vertex AI · text+image 768D)", C_WORKER, 9)
box(7.4, 4.4, 2.6, 1.2,
    "📬 semantic_queue", C_QUEUE, 10)
box(10.7, 4.4, 3.0, 1.2,
    "🧠 semantic-worker\n(Groq attribute extract)", C_WORKER, 9)
box(14.5, 4.4, 2.5, 1.2,
    "🌐 Vertex AI\n(text+image)", C_EXT, 9)

# ── Row 5: Matching + Suggestion ───────────────────────────────────────────
box(0.3, 2.2, 2.6, 1.2,
    "📬 match_queue", C_QUEUE, 10)
box(3.6, 2.2, 3.0, 1.2,
    "🎯 matcher-worker\n(pgvector cosine sim)", C_WORKER, 10)
box(7.4, 2.2, 2.6, 1.2,
    "📬 suggestion_queue", C_QUEUE, 10)
box(10.7, 2.2, 3.0, 1.2,
    "💡 suggestion-worker\n(Groq pricing advice)", C_WORKER, 10)
box(14.5, 2.2, 5.2, 1.2,
    "📊 UI: Matched Products · Price Comparison · Suggestions",
    C_UI, 10)

# ── Row 6: Footer / Redis ──────────────────────────────────────────────────
box(7.0, 0.3, 6.0, 1.0,
    "🟥 Redis  —  Celery broker + result backend for ALL queues above",
    "#FFCDD2", 10)

# ── Arrows ─────────────────────────────────────────────────────────────────
# Merchant → UI → DB
arrow(2.9, 11.85, 3.6, 11.85, "configures")
arrow(7.2, 11.85, 8.0, 11.85, "REST")
arrow(11.6, 11.85, 12.4, 11.85, "Prisma JS")
arrow(5.4, 11.2, 5.4, 10.0, "ScrapingConfig\n→ DB", fs=7, label_offset=(0.6, 0))

# Beat → scheduler_queue → scheduler-worker
arrow(2.9, 9.4, 3.6, 9.4, "tick")
arrow(6.0, 9.4, 6.6, 9.4, "pop")

# scheduler-worker → scraping_queue (down-left)
arrow(8.1, 8.8, 2.0, 7.8, "enqueue\nscrape jobs", curve=-0.2, fs=8)

# scraping_queue → scraper-worker
arrow(2.9, 7.2, 3.6, 7.2, "pop")
# scraper-worker uses Firecrawl
arrow(6.6, 7.2, 14.5, 7.0, "scrape", curve=0.2, ls="--", fs=7)
# scraper-worker → GCS markdown
arrow(5.1, 7.8, 17.5, 11.2, "markdown.md", curve=-0.25, ls="--", fs=7)
# scraper-worker → extraction_queue
arrow(6.6, 7.2, 7.4, 7.2, "")

# extraction-worker pulls
arrow(10.0, 7.2, 10.7, 7.2, "pop")
# extraction-worker uses Groq
arrow(13.7, 7.2, 17.2, 7.0, "extract", ls="--", fs=7)
# extraction-worker → DB (ScrapedProduct)
arrow(12.2, 7.8, 14.1, 11.2, "ScrapedProduct\nScrapedVariant", curve=0.15, fs=7)
# extraction-worker → GCS images
arrow(13.0, 7.8, 17.9, 11.2, "images", curve=-0.2, ls="--", fs=7)
# extraction-worker → embedding_queue + semantic_queue
arrow(11.0, 6.6, 2.0, 5.6, "fan-out", curve=-0.3, fs=8)
arrow(11.5, 6.6, 8.7, 5.6, "fan-out", curve=0.1, fs=8)

# embedding pipeline
arrow(2.9, 5.0, 3.6, 5.0, "pop")
arrow(6.6, 5.0, 14.5, 5.0, "embed", ls="--", fs=7)
# embedding-worker → DB (raw SQL pgvector)
arrow(5.1, 5.6, 14.1, 11.2, "pgvector\n(raw SQL)", curve=0.25, fs=7)
# embedding → match_queue
arrow(5.1, 4.4, 2.0, 3.4, "trigger\nmatch", curve=-0.25, fs=8)

# semantic pipeline
arrow(10.0, 5.0, 10.7, 5.0, "pop")
arrow(12.2, 4.4, 14.1, 11.2, "attributes\n→ DB", curve=0.25, ls=":", fs=7)

# matcher pipeline
arrow(2.9, 2.8, 3.6, 2.8, "pop")
arrow(6.6, 2.8, 7.4, 2.8, "ProductMatch")
arrow(5.1, 3.4, 14.1, 11.2, "MatchScore\n→ DB", curve=0.3, ls=":", fs=7)

# suggestion pipeline
arrow(10.0, 2.8, 10.7, 2.8, "pop")
arrow(13.7, 2.8, 14.5, 2.8, "shown to merchant")
arrow(12.2, 3.4, 14.1, 11.2, "Suggestion\n→ DB", curve=0.3, ls=":", fs=7)

# UI reads back results
arrow(17.0, 3.4, 14.5, 11.2, "Prisma reads\nfor UI", curve=-0.35, ls="--", fs=7)

# ── Legend ─────────────────────────────────────────────────────────────────
legend_items = [
    ("User / UI",    C_UI),
    ("Worker (Celery)", C_WORKER),
    ("Queue (Redis)", C_QUEUE),
    ("Beat / Scheduler", C_BEAT),
    ("Datastore",    C_STORE),
    ("External API", C_EXT),
]
handles = [mpatches.Patch(facecolor=c, edgecolor="#333", label=lbl)
           for lbl, c in legend_items]
ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.0, -0.02),
          ncol=6, fontsize=9, frameon=True)

plt.tight_layout()
plt.savefig(OUT, dpi=160, bbox_inches="tight", facecolor="white")
print(f"Wrote {OUT}")
