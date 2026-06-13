# Chatbot History-Aware Enable + Chatbot-as-Homepage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the chatbot's dynamic-pricing enable card reflect a product's history (fresh / paused-with-data / active), and make the chatbot the app homepage at `/app`.

**Architecture:** A new read-only resolver (`resolve_enable_context`) derives state purely from existing rows (`CompetitorCandidate` presence + flag) and composes existing helpers. `preview_dynamic_pricing_toggle` calls it internally and embeds a snapshot in the preview `summary`; the card renders that snapshot plus a new frequency control. Routing change relocates the Products list to `/app/products` and renders `<ChatPanel/>` at `/app`, moving the fresh-install sync bootstrap into the new index loader.

**Tech Stack:** Python (SQLAlchemy, pydantic, pydantic-ai agent, pytest, uv), React Router 7 (JSX, Prisma JS), Shopify Polaris web components.

**Commit style:** simple one-line messages, no co-author/attribution line.

---

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `services/common/models.py` | Modify | Add `ProductLevelMatch` ORM model (minimal) + `_match_confidence_tier` enum |
| `services/chatbot_svc/schemas.py` | Modify | Add `EnableContext` pydantic model |
| `services/chatbot_svc/tools/enable_context.py` | Create | `resolve_enable_context` — derives state + snapshot |
| `services/chatbot_svc/tools/preview.py` | Modify | Branch `preview_dynamic_pricing_toggle` on state; embed snapshot in `summary` |
| `services/chatbot_svc/prompts/system.md` | Modify | Teach the agent the three states + free-vs-paid actions |
| `services/chatbot_svc/tests/test_enable_context.py` | Create | Resolver unit tests |
| `services/chatbot_svc/tests/test_preview_history.py` | Create | Preview branch tests |
| `shopify_ui/app/components/chatbot/DynamicPricingCard.jsx` | Modify | Snapshot banner + frequency input on paused-with-data |
| `shopify_ui/app/routes/internal.apply-chat-flag.jsx` | Modify | Persist frequency on resume; apply-time state re-check |
| `shopify_ui/app/routes/app.products.jsx` | Create | Relocated Products list (old index, minus bootstrap) |
| `shopify_ui/app/routes/app._index.jsx` | Replace | Chatbot homepage + sync bootstrap |
| `shopify_ui/app/routes/app.chatbot.jsx` | Modify | Redirect to `/app` |
| `shopify_ui/app/routes/app.jsx` | Modify | Nav: Products → `/app/products`; home = Assistant |

---

# Part A — History-aware enable flow

## Task 1: Add `ProductLevelMatch` ORM model

**Files:**
- Modify: `services/common/models.py` (enum block near line 42; new model class after `ProductMatch` at line 320)

- [ ] **Step 1: Add the enum** after `_discovery_status` (after line 46):

```python
_match_confidence_tier = PgEnum(
    "CONFIRMED", "LIKELY", "WEAK",
    name="MatchConfidenceTier",
    create_type=False,
)
```

- [ ] **Step 2: Add the model** immediately after the `ProductMatch` class (after line 320, before the `# Competitor discovery` comment). Mirror the partial-mapping style ProductMatch uses — only the columns the resolver reads:

```python
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
    confidenceTier   = Column("confidenceTier",   _match_confidence_tier, nullable=False)
    rejectedByMerchant = Column("rejectedByMerchant", Boolean, nullable=False, default=False)
```

- [ ] **Step 3: Verify it imports**

Run: `uv run python -c "from services.common.models import ProductLevelMatch; print(ProductLevelMatch.__tablename__)"`
Expected: prints `ProductLevelMatch`

- [ ] **Step 4: Commit**

```bash
git add services/common/models.py
git commit -m "add ProductLevelMatch ORM model for chatbot match counts"
```

---

## Task 2: Add `EnableContext` schema

**Files:**
- Modify: `services/chatbot_svc/schemas.py` (after `DynamicPricingStatus`, line 61)

- [ ] **Step 1: Add the schema** after the `DynamicPricingStatus` class:

```python
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
```

- [ ] **Step 2: Verify import**

Run: `uv run python -c "from services.chatbot_svc.schemas import EnableContext; print(EnableContext(product_id='p', state='FRESH'))"`
Expected: prints a model instance with defaults

- [ ] **Step 3: Commit**

```bash
git add services/chatbot_svc/schemas.py
git commit -m "add EnableContext schema for history-aware enable card"
```

---

## Task 3: `resolve_enable_context` resolver + tests

**Files:**
- Create: `services/chatbot_svc/tools/enable_context.py`
- Test: `services/chatbot_svc/tests/test_enable_context.py`

- [ ] **Step 1: Write the failing tests**

```python
# services/chatbot_svc/tests/test_enable_context.py
import uuid

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct, CompetitorCandidate, DiscoveryJob,
    ProductUrl, ProductLevelMatch, ScrapedProduct,
)
from services.chatbot_svc.tools.enable_context import resolve_enable_context


def _product_id(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(
            ShopifyProduct.shopDomain == shop
        ).scalar()


def test_fresh_when_no_candidates(seed_shop):
    pid = _product_id(seed_shop)
    ctx = resolve_enable_context(seed_shop, pid)
    assert ctx.state == "FRESH"
    assert ctx.competitors_found == 0
    # current_query falls back to the product title
    assert ctx.current_query == "Boat Speaker White"


def test_active_when_flag_on(seed_shop):
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.get(ShopifyProduct, pid).dynamicPricingEnabled = True
    ctx = resolve_enable_context(seed_shop, pid)
    assert ctx.state == "ACTIVE"


def test_paused_with_data_when_candidates_exist(seed_shop):
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.add(CompetitorCandidate(
            id=str(uuid.uuid4()), shopDomain=seed_shop, shopifyProductId=pid,
            url="https://x.test/p", domain="x.test", source="serper_search",
            status="SCRAPED",
        ))
    ctx = resolve_enable_context(seed_shop, pid)
    assert ctx.state == "PAUSED_WITH_DATA"
    assert ctx.competitors_found == 1


def test_query_drift_detected(seed_shop):
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.add(CompetitorCandidate(
            id=str(uuid.uuid4()), shopDomain=seed_shop, shopifyProductId=pid,
            url="https://x.test/p", domain="x.test", source="serper_search",
            status="SCRAPED",
        ))
        s.add(DiscoveryJob(
            id=str(uuid.uuid4()), shopDomain=seed_shop, shopifyProductId=pid,
            status="COMPLETED", query="old query",
        ))
        s.get(ShopifyProduct, pid).searchQueryOverride = "new query"
    ctx = resolve_enable_context(seed_shop, pid)
    assert ctx.existing_query == "old query"
    assert ctx.current_query == "new query"
    assert ctx.query_drifted is True


def test_live_matches_excludes_rejected_and_weak(seed_shop):
    pid = _product_id(seed_shop)
    with get_db() as s:
        sp = ScrapedProduct(
            id=str(uuid.uuid4()), shopDomain=seed_shop, domain="x.test",
            title="comp",
        )
        s.add(sp)
        s.flush()
        common = dict(shopDomain=seed_shop, shopifyProductId=pid,
                      scrapedProductId=sp.id)
        s.add(ProductLevelMatch(id=str(uuid.uuid4()), confidenceTier="LIKELY",
                                rejectedByMerchant=False, **common))
        # rejected → excluded
        sp2 = ScrapedProduct(id=str(uuid.uuid4()), shopDomain=seed_shop,
                             domain="y.test", title="comp2")
        s.add(sp2); s.flush()
        s.add(ProductLevelMatch(id=str(uuid.uuid4()), confidenceTier="LIKELY",
                                rejectedByMerchant=True, shopDomain=seed_shop,
                                shopifyProductId=pid, scrapedProductId=sp2.id))
        # WEAK → excluded
        sp3 = ScrapedProduct(id=str(uuid.uuid4()), shopDomain=seed_shop,
                             domain="z.test", title="comp3")
        s.add(sp3); s.flush()
        s.add(ProductLevelMatch(id=str(uuid.uuid4()), confidenceTier="WEAK",
                                rejectedByMerchant=False, shopDomain=seed_shop,
                                shopifyProductId=pid, scrapedProductId=sp3.id))
    ctx = resolve_enable_context(seed_shop, pid)
    assert ctx.live_matches == 1
```

> Note: `ScrapedProduct` and `ProductLevelMatch` carry extra columns in Prisma but the ORM maps only the subset these tasks use. If a DB insert fails on a NOT-NULL column that the ORM doesn't map, that column has a DB-side default or the row needs it — add the minimal value to the test row. Only pass kwargs for columns mapped in Task 1 (`confidence` is intentionally not mapped, so it is not passed here).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest services/chatbot_svc/tests/test_enable_context.py -v`
Expected: FAIL — `ModuleNotFoundError: ... enable_context`

- [ ] **Step 3: Implement the resolver**

```python
# services/chatbot_svc/tools/enable_context.py
from __future__ import annotations

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct, CompetitorCandidate, DiscoveryJob,
    ProductUrl, ProductLevelMatch,
)
from services.chatbot_svc.schemas import EnableContext
from services.chatbot_svc.tools.toggle_settings import resolve_enable_settings


def resolve_enable_context(shop_domain: str, product_id: str) -> EnableContext | None:
    """Derive the enable-card context for one product. Read-only.

    Returns None if the product does not belong to shop_domain.
    """
    settings = resolve_enable_settings(shop_domain, [product_id])

    with get_db() as s:
        p = s.get(ShopifyProduct, product_id)
        if p is None or p.shopDomain != shop_domain:
            return None

        competitors_found = (
            s.query(CompetitorCandidate)
            .filter(CompetitorCandidate.shopDomain == shop_domain,
                    CompetitorCandidate.shopifyProductId == product_id)
            .count()
        )
        live_matches = (
            s.query(ProductLevelMatch)
            .filter(ProductLevelMatch.shopDomain == shop_domain,
                    ProductLevelMatch.shopifyProductId == product_id,
                    ProductLevelMatch.rejectedByMerchant.is_(False),
                    ProductLevelMatch.confidenceTier.in_(("CONFIRMED", "LIKELY")))
            .count()
        )
        dead_links = (
            s.query(ProductUrl)
            .filter(ProductUrl.shopifyProductId == product_id,
                    ProductUrl.status == "DEAD")
            .count()
        )
        latest_job = (
            s.query(DiscoveryJob)
            .filter(DiscoveryJob.shopDomain == shop_domain,
                    DiscoveryJob.shopifyProductId == product_id)
            .order_by(DiscoveryJob.requestedAt.desc())
            .first()
        )
        existing_query = latest_job.query if latest_job else None
        last = p.lastDiscoveryAt or (latest_job.completedAt if latest_job else None)
        enabled = bool(p.dynamicPricingEnabled)

    current_query = settings["query"]
    query_drifted = bool(existing_query) and existing_query != current_query

    if enabled:
        state = "ACTIVE"
    elif competitors_found == 0:
        state = "FRESH"
    else:
        state = "PAUSED_WITH_DATA"

    return EnableContext(
        product_id=product_id,
        state=state,
        competitors_found=competitors_found,
        live_matches=live_matches,
        last_discovery_at=last.isoformat() if last else None,
        existing_query=existing_query,
        current_query=current_query,
        query_drifted=query_drifted,
        dead_links=dead_links,
        num_results=settings["numResults"],
        listing_expansion_cap=settings["listingExpansionCap"],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest services/chatbot_svc/tests/test_enable_context.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add services/chatbot_svc/tools/enable_context.py services/chatbot_svc/tests/test_enable_context.py
git commit -m "add resolve_enable_context resolver for history-aware enable"
```

---

## Task 4: Branch the preview on state + embed snapshot

**Files:**
- Modify: `services/chatbot_svc/tools/preview.py` (`preview_dynamic_pricing_toggle`, lines 78–147)
- Test: `services/chatbot_svc/tests/test_preview_history.py`

- [ ] **Step 1: Write the failing tests**

```python
# services/chatbot_svc/tests/test_preview_history.py
import uuid

from services.common.db import get_db
from services.common.models import ShopifyProduct, CompetitorCandidate
from services.chatbot_svc.schemas import ScopeFilter
from services.chatbot_svc.tools.preview import preview_dynamic_pricing_toggle


def _pid(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(
            ShopifyProduct.shopDomain == shop).scalar()


def _scope(pid):
    return ScopeFilter(product_ids=[pid])


def _stored_summary(preview_id):
    from services.common.models import ChatPreview
    with get_db() as s:
        return s.get(ChatPreview, preview_id).summary


def test_fresh_preview_has_fresh_state(seed_shop):
    pid = _pid(seed_shop)
    res = preview_dynamic_pricing_toggle(seed_shop, "sess1", _scope(pid), True)
    summary = _stored_summary(res.preview_id)
    assert summary["enableContext"]["state"] == "FRESH"
    assert "first time" in res.human_summary.lower() or "set up" in res.human_summary.lower()


def test_paused_preview_reports_existing_data(seed_shop):
    pid = _pid(seed_shop)
    with get_db() as s:
        s.add(CompetitorCandidate(
            id=str(uuid.uuid4()), shopDomain=seed_shop, shopifyProductId=pid,
            url="https://x.test/p", domain="x.test", source="serper_search",
            status="SCRAPED"))
    res = preview_dynamic_pricing_toggle(seed_shop, "sess2", _scope(pid), True)
    summary = _stored_summary(res.preview_id)
    assert summary["enableContext"]["state"] == "PAUSED_WITH_DATA"
    assert summary["enableContext"]["competitors_found"] == 1
    assert "1" in res.human_summary  # mentions the existing competitor count
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest services/chatbot_svc/tests/test_preview_history.py -v`
Expected: FAIL — `KeyError: 'enableContext'`

- [ ] **Step 3: Implement the branch.** In `preview.py`, add the import at the top with the other tool imports:

```python
from services.chatbot_svc.tools.enable_context import resolve_enable_context
```

Then, inside `preview_dynamic_pricing_toggle`, in the `if enabled:` branch (currently lines 102–118), replace the body with:

```python
    if enabled:
        settings = resolve_enable_settings(shop_domain, product_ids)
        ctx = resolve_enable_context(shop_domain, product_ids[0]) if product_ids else None
        ctx_dict = ctx.model_dump() if ctx else {"state": "FRESH"}
        change = {
            "enabled": True,
            "rescrape": False,
            "numResults": settings["numResults"],
            "listingExpansionCap": settings["listingExpansionCap"],
            "query": settings["query"],
        }
        summary_dict["enable"] = settings
        summary_dict["enableContext"] = ctx_dict

        if ctx_dict["state"] == "PAUSED_WITH_DATA":
            human = (
                f"This product was set up before — you already have "
                f"{ctx_dict['competitors_found']} competitor(s) "
                f"({ctx_dict['live_matches']} matched). Resume with those for free, "
                f"or spend a fresh fetch to find new ones or widen the search. "
                f"Confirm below."
            )
        else:  # FRESH (ACTIVE products are handled by the agent via status, not here)
            human = (
                f"Setting up dynamic pricing for the first time on "
                f"{len(product_ids)} product(s). The first competitor fetch will "
                f"search ~{settings['numResults']} sites and up to "
                f"{settings['listingExpansionCap']} products per listing page — "
                f"editable below. Confirm to start."
            )
```

> The `else` (disable) branch and the trailing `ChatPreview` insert / `return` are unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest services/chatbot_svc/tests/test_preview_history.py services/chatbot_svc/tests/test_enable_context.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/chatbot_svc/tools/preview.py services/chatbot_svc/tests/test_preview_history.py
git commit -m "branch dynamic-pricing preview on product history state"
```

---

## Task 5: Card renders snapshot + frequency input

**Files:**
- Modify: `shopify_ui/app/components/chatbot/DynamicPricingCard.jsx`

- [ ] **Step 1: Add frequency state** after line 19 (`const [cap, ...]`):

```jsx
  // Resume cadence (paused-with-data only). "" / "nofreq" => no schedule change.
  const [freqInterval, setFreqInterval] = useState("");
  const [freqUnit, setFreqUnit] = useState("daily");
```

- [ ] **Step 2: Read the context** after line 24 (`const dc = ...`):

```jsx
  const ctx = summary.enableContext || {};
  const paused = ctx.state === "PAUSED_WITH_DATA";
```

- [ ] **Step 3: Pass frequency through on apply.** Replace the `if (enable)` block inside `apply` (lines 34–41) with:

```jsx
    if (enable) {
      onApply(preview, {
        enable: true,
        rescrape,
        numResults: clamp(num, 1, 50, 10),
        listingExpansionCap: clamp(cap, 1, 50, 5),
        query: query.trim(),
        frequencyInterval: freqInterval === "" ? null : clamp(freqInterval, 1, 365, 1),
        frequencyUnit: freqInterval === "" ? null : freqUnit,
      });
    } else {
```

- [ ] **Step 4: Render the snapshot banner** directly after the `{count} products` line (after line 53):

```jsx
        {paused && (
          <s-banner tone="info">
            <s-text>
              {`Already set up: ${ctx.competitors_found ?? 0} competitor(s), `}
              {`${ctx.live_matches ?? 0} matched`}
              {ctx.last_discovery_at ? ` · last fetched ${new Date(ctx.last_discovery_at).toLocaleDateString()}` : ""}
              {ctx.dead_links ? ` · ${ctx.dead_links} dead link(s)` : ""}
            </s-text>
            {ctx.query_drifted && (
              <s-text tone="subdued">
                {`Existing competitors were found with "${ctx.existing_query}". Your current query is "${ctx.current_query}". Resume keeps the old set; edit the query below to find a new one.`}
              </s-text>
            )}
          </s-banner>
        )}
```

- [ ] **Step 5: Render the frequency control** inside the `enable ?` block, after the search-query field (after line 87, before the Query Studio button):

```jsx
            {paused && (
              <s-stack direction="inline" gap="base" align="end">
                <s-text-field
                  label="Auto-refresh every (blank = no schedule)"
                  type="number" value={freqInterval} min="1" max="365"
                  onInput={(e) => setFreqInterval(e.currentTarget.value)}
                />
                <s-select
                  label="Unit"
                  value={freqUnit}
                  onChange={(e) => setFreqUnit(e.currentTarget.value)}
                >
                  <s-option value="hourly">Hours</s-option>
                  <s-option value="daily">Days</s-option>
                  <s-option value="weekly">Weeks</s-option>
                </s-select>
              </s-stack>
            )}
```

- [ ] **Step 6: Verify typecheck + lint**

Run: `cd shopify_ui && npm run lint && npm run typecheck`
Expected: no errors in `DynamicPricingCard.jsx`

- [ ] **Step 7: Commit**

```bash
git add shopify_ui/app/components/chatbot/DynamicPricingCard.jsx
git commit -m "show history snapshot and resume cadence on enable card"
```

---

## Task 6: Apply route — persist frequency + apply-time re-check

**Files:**
- Modify: `shopify_ui/app/routes/internal.apply-chat-flag.jsx`

- [ ] **Step 1: Persist frequency on enable.** Inside the `if (enabled)` block, after the existing `searchQueryOverride` update (after line 100), add:

```jsx
    // Resume cadence chosen on the card (paused-with-data). Null clears any
    // per-product override and falls back to ShopSettings cadence.
    if (body.frequencyInterval != null) {
      await prisma.shopifyProduct.updateMany({
        where: { id: { in: productIds }, shopDomain },
        data: {
          frequencyInterval: parseInt(body.frequencyInterval, 10),
          frequencyUnit: body.frequencyUnit || "daily",
        },
      });
    }
```

- [ ] **Step 2: Add an apply-time staleness guard.** Immediately after the `preview.kind !== "dynamic_pricing_toggle"` check (after line 52), add a re-read of current flag state so a flip between preview and apply is caught:

```jsx
  // Apply-time re-check: the flag may have changed since the preview card was
  // shown (the merchant could have toggled it elsewhere). If the DB already
  // matches the requested target, report it rather than redo the work.
  {
    const targetEnabled = !!preview.change?.enabled;
    const current = await prisma.shopifyProduct.findMany({
      where: { id: { in: preview.variantIds }, shopDomain: preview.shopDomain },
      select: { id: true, dynamicPricingEnabled: true },
    });
    const allAlready = current.length > 0 && current.every(
      (p) => p.dynamicPricingEnabled === targetEnabled,
    );
    if (allAlready) {
      await prisma.chatPreview.update({
        where: { id: preview_id },
        data: { appliedAt: new Date(), appliedBy: applied_by ?? null,
                result: { noop: true, enabled: targetEnabled } },
      });
      return Response.json({ ok: true, preview_id, noop: true, enabled: targetEnabled });
    }
  }
```

- [ ] **Step 3: Verify typecheck + lint**

Run: `cd shopify_ui && npm run lint && npm run typecheck`
Expected: no errors in `internal.apply-chat-flag.jsx`

- [ ] **Step 4: Commit**

```bash
git add shopify_ui/app/routes/internal.apply-chat-flag.jsx
git commit -m "persist resume cadence and add apply-time flag re-check"
```

---

## Task 7: Update the system prompt

**Files:**
- Modify: `services/chatbot_svc/prompts/system.md`

- [ ] **Step 1: Find the dynamic-pricing toggle guidance.** Read the file and locate the section describing `preview_dynamic_pricing_toggle` / enabling dynamic pricing.

Run: `grep -n "dynamic pricing\|preview_dynamic_pricing_toggle\|enable" services/chatbot_svc/prompts/system.md`

- [ ] **Step 2: Add this guidance** in that section (adapt wording to the file's voice):

```markdown
When enabling dynamic pricing, the preview card is now history-aware (it reads
`summary.enableContext.state`):

- **FRESH** (never set up): describe it as a first-time setup that will scan
  competitor sites.
- **PAUSED_WITH_DATA** (was on before, data kept): tell the merchant they
  already have competitors from before (`competitors_found` / `live_matches`).
  Make clear that **Resume keeps the existing competitors at no extra fetch
  cost**, while **finding a new set or widening the search spends a fresh
  competitor fetch**. If `query_drifted` is true, point out the query changed.
- **ACTIVE** (already on): do NOT offer to enable again. Call
  `get_dynamic_pricing_status` and report the current status instead.

Do not claim a product is being set up "for the first time" unless the state is
FRESH.
```

- [ ] **Step 3: Verify the file still reads coherently**

Run: `grep -n "PAUSED_WITH_DATA\|FRESH\|ACTIVE" services/chatbot_svc/prompts/system.md`
Expected: the new guidance is present

- [ ] **Step 4: Commit**

```bash
git add services/chatbot_svc/prompts/system.md
git commit -m "teach agent the three dynamic-pricing enable states"
```

---

# Part B — Chatbot as app homepage

## Task 8: Relocate the Products list to `/app/products`

**Files:**
- Create: `shopify_ui/app/routes/app.products.jsx`
- Reference: current `shopify_ui/app/routes/app._index.jsx`

- [ ] **Step 1: Copy the current index to the new route.**

Run: `cp shopify_ui/app/routes/app._index.jsx shopify_ui/app/routes/app.products.jsx`

- [ ] **Step 2: Remove the sync-bootstrap block from `app.products.jsx`'s loader.** Open `app.products.jsx`, find the "Non-blocking auto-kick" block (the `fetch(PYTHON_API_URL ...)` enqueue guarded by `count === 0 || user?.productSyncedAt == null`) and delete it. Keep the `ShopifyUser` upsert and all product/read queries — the bootstrap moves to the index in Task 9. The component (default export) is unchanged.

- [ ] **Step 3: Verify typecheck + lint**

Run: `cd shopify_ui && npm run lint && npm run typecheck`
Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add shopify_ui/app/routes/app.products.jsx
git commit -m "add /app/products route with the products list"
```

---

## Task 9: Make `/app` the chatbot; redirect old route; update nav

**Files:**
- Replace: `shopify_ui/app/routes/app._index.jsx`
- Modify: `shopify_ui/app/routes/app.chatbot.jsx`
- Modify: `shopify_ui/app/routes/app.jsx`

- [ ] **Step 1: Replace `app._index.jsx`** with a chatbot homepage that keeps the fresh-install bootstrap (Option A). Carry over the exact bootstrap logic from the old index loader (ShopifyUser upsert + non-blocking sync enqueue):

```jsx
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";
import ChatPanel from "../components/chatbot/ChatPanel";

// Homepage = the assistant. The loader keeps the fresh-install sync bootstrap
// that used to live on the products list, so a never-synced store still kicks
// a background product pull when the merchant lands here.
export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  await db.shopifyUser.upsert({
    where: { shopDomain },
    update: {},
    create: { shopDomain },
  });

  const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
  const user = await db.shopifyUser.findUnique({ where: { shopDomain } });
  const count = await db.shopifyProduct.count({ where: { shopDomain } });
  if (
    (count === 0 || user?.productSyncedAt == null) &&
    user?.productSyncState !== "SYNCING" &&
    user?.productSyncState !== "ERROR"
  ) {
    await db.shopifyUser.update({
      where: { shopDomain },
      data: { productSyncState: "SYNCING", productSyncStartedAt: new Date() },
    });
    // Fire-and-forget; never await — the loader stays read-only/non-blocking.
    void fetch(
      `${PYTHON_API_URL}/internal/shopify/sync-products?shop_domain=${encodeURIComponent(shopDomain)}`,
      { method: "POST" },
    ).catch(() => {});
  }

  return {};
};

export default function HomePage() {
  return (
    <s-page heading="Assistant">
      <ChatPanel />
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
```

> This bootstrap is copied verbatim from the pre-change `app._index.jsx` loader (endpoint `/internal/shopify/sync-products`, `productSyncState: "SYNCING"` set first). Confirm with `git show HEAD:shopify_ui/app/routes/app._index.jsx` if the original has drifted.

- [ ] **Step 2: Redirect the old chatbot route.** Replace `shopify_ui/app/routes/app.chatbot.jsx` with:

```jsx
import { redirect } from "react-router";

export const loader = () => redirect("/app");
```

- [ ] **Step 3: Update the nav** in `shopify_ui/app/routes/app.jsx` (lines 18–25). Replace the `<s-app-nav>` block with:

```jsx
      <s-app-nav>
        <s-link href="/app">Assistant</s-link>
        <s-link href="/app/products">Products</s-link>
        <s-link href="/app/matches">Matched competitors</s-link>
        <s-link href="/app/stats">Stats</s-link>
        <s-link href="/app/settings">Settings</s-link>
        <s-link href="/app/suggestions">Product suggestions</s-link>
      </s-app-nav>
```

- [ ] **Step 4: Verify build + typecheck + lint**

Run: `cd shopify_ui && npm run lint && npm run typecheck && npm run build`
Expected: clean build; `/app`, `/app/products`, `/app/chatbot` all resolve

- [ ] **Step 5: Manual verification**

Run `npm run dev`, then in the embedded app:
- `/app` shows the chatbot.
- `/app/products` shows the products list with toggles.
- `/app/chatbot` redirects to `/app`.
- On a fresh/never-synced store, landing on `/app` enqueues a product pull (check worker logs / `productSyncState`).

- [ ] **Step 6: Commit**

```bash
git add shopify_ui/app/routes/app._index.jsx shopify_ui/app/routes/app.chatbot.jsx shopify_ui/app/routes/app.jsx
git commit -m "make chatbot the app homepage; move products to /app/products"
```

---

## Final verification

- [ ] **Run the chatbot test suite**

Run: `uv run pytest services/chatbot_svc/tests/ -v`
Expected: all pass, including the new `test_enable_context.py` and `test_preview_history.py`

- [ ] **Frontend gate**

Run: `cd shopify_ui && npm run lint && npm run typecheck && npm run build`
Expected: clean

- [ ] **Smoke the real flow** (optional but recommended): enable dynamic pricing via the chatbot for a product that already has competitor data (e.g. the Fevicol product) and confirm the card shows the "already set up: N competitors" snapshot instead of first-time copy.

---

## Notes / decisions baked in

- **Derived state, no new column** — `FRESH`/`PAUSED_WITH_DATA`/`ACTIVE` come from `CompetitorCandidate` presence + the flag.
- **`CompetitorCandidate` is the "set up before" signal** (product-scoped, always deleted on teardown), not `ScrapedProduct` (shared-row guard keeps some).
- **Resume is free; new-set/widen spend a fetch** — expressed through the existing `rescrape`/`query`/`numResults` card controls; only the frequency input is new.
- **Sync bootstrap = Option A** — moved into the new `/app` (chatbot) loader.
- **UI toggle pages unchanged** — they already show real data and reuse prior state.
