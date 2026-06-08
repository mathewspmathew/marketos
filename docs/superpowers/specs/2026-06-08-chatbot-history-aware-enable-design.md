# Chatbot history-aware dynamic-pricing enable + chatbot-as-homepage

Date: 2026-06-08
Status: Draft for review

## Summary

Two related changes to the chatbot surface:

1. **History-aware enable flow** — when a merchant asks the chatbot to turn on
   dynamic pricing, the enable card must reflect the product's history instead
   of always showing generic first-time copy ("I'll scan ~10 sites…"). It must
   distinguish a never-set-up product, a paused product whose competitor data
   still exists, and a product that is already active.
2. **Chatbot as app homepage** — make the chatbot the landing page at `/app`,
   relocating the current Products list to `/app/products`.

The two are bundled because the second makes the chatbot the merchant's primary
surface, which is exactly why the first (the chatbot reading product history
before it speaks) matters.

This is **chatbot-only**. The main UI toggle (`app.discover.$id.jsx`) already
shows real status + existing candidates and reuses prior data on toggle-on, so
it is inherently history-aware and is **not changed** by this spec.

---

## Feature 1 — History-aware enable flow

### Problem

`preview_dynamic_pricing_toggle` (`services/chatbot_svc/tools/preview.py`) builds
the same enable card regardless of the product's past. The copy comes from the
LLM system prompt (`prompts/system.md`) and `preview.py`'s `human` string. A
product that was enabled, paused, and still has 7 scraped competitors reads as a
brand-new setup. The merchant has no way to see "you already have data" or to
choose "resume with what I have" vs "find a fresh set."

### Core principle

**State is derived, not stored.** Do not add a `wasPreviouslyEnabled` column.
The three states are computable from existing rows:

| State | Condition |
|-------|-----------|
| `ACTIVE` | `dynamicPricingEnabled = true` |
| `FRESH` | flag off **and** this product has **0** `CompetitorCandidate` rows |
| `PAUSED_WITH_DATA` | flag off **and** `CompetitorCandidate` rows exist |

`CompetitorCandidate` presence (not `ScrapedProduct`) is the "set up before"
signal: candidates are product-scoped and always removed on teardown, whereas
`ScrapedProduct` rows can survive deletion because of the shared-row guard in
`compute_disable_counts`.

### New component: `resolve_enable_context`

A single read-only resolver in a new module
`services/chatbot_svc/tools/enable_context.py`:

```
resolve_enable_context(shop_domain, product_id) -> EnableContext
```

`EnableContext` (pydantic, in `chatbot_svc/schemas.py`):

- `state`: `"FRESH" | "PAUSED_WITH_DATA" | "ACTIVE"`
- `competitors_found`: int  — `CompetitorCandidate` count for this product
- `live_matches`: int  — `ProductLevelMatch` with tier in (CONFIRMED, LIKELY)
  and `rejectedByMerchant = false` (mirrors the Matched Competitors page)
- `last_discovery_at`: iso str | null
- `existing_query`: str | null  — query of the latest `DiscoveryJob`
- `current_query`: str  — `searchQueryOverride || searchQuery || title`
- `query_drifted`: bool  — `existing_query` present and differs from `current_query`
- `dead_links`: int  — `ProductUrl` rows with `status = DEAD`
- `settings`: `{ numResults, listingExpansionCap }` from `resolve_enable_settings`

The resolver **composes existing helpers** — `get_dynamic_pricing_status`
(`tools/status.py`) for the active-side detail, `resolve_enable_settings`
(`tools/toggle_settings.py`) for breadth defaults — rather than re-querying.

### Card behaviour per state

**FRESH** — unchanged from today. First-time copy + breadth defaults.
`change = { enabled, rescrape, numResults, listingExpansionCap, query }`.

**PAUSED_WITH_DATA** — show snapshot ("N competitors from before, M matched,
last fetched <date>") and offer four explicit actions:

| Action | Effect | Spends scrape credits? |
|--------|--------|------------------------|
| Resume as-is | enable + re-arm existing `ProductUrl`s | No |
| Resume + set frequency | enable + re-arm + set `frequencyInterval/Unit` (editable) | No |
| Find a new set | enable + set `searchQueryOverride` + create `DiscoveryJob` | Yes |
| Widen | enable + raise `discoveryNumResults`/`listingExpansionCap` + create `DiscoveryJob` | Yes |

The free vs paid distinction must be explicit on the card.

**ACTIVE** — do **not** render an enable card. Show the live status from
`get_dynamic_pricing_status` and offer manage actions (pause, re-discover,
widen).

### Edge cases and their solutions

| # | Edge case | Solution |
|---|-----------|----------|
| 1 | "Deleted" leaves shared competitor rows behind | Use **`CompetitorCandidate` count** (product-scoped, always deleted) as the FRESH signal, never `ScrapedProduct`. |
| 2 | "7 found, 0 matched" looks broken | Render words, not bare counts, via the existing status state (`PROCESSING` vs `NEEDS_ATTENTION`). The matcher gate fix already moves Fevicol to 7-found/7-matched. |
| 3 | Paused mid-discovery → partial data | Show the honest scraped count (only SCRAPED candidates; dropped PENDING ones simply aren't counted). Refilling is the existing paid "Find a new set" / "Widen" action — no separate top-up action. |
| 4 | Stale prices / dead links after long pause | Surface `dead_links` and `last_discovery_at`; when links are dead or data is old, default the card to "Resume + refresh now" and skip DEAD URLs. |
| 5 | Existing competitors found with an old query | Show `existing_query` vs `current_query`; when `query_drifted`, "Resume as-is" keeps the old set, "Find new set" uses the current query. |
| 6 | Previously rejected matches | `live_matches` counts only `rejectedByMerchant = false`; resume never resurrects rejected rows. |
| 7 | Card snapshot stale before Confirm | Apply route **re-reads state at apply time** (flag + candidate presence). If it changed materially, return a soft signal so the bot re-confirms instead of acting on the stale snapshot. |
| 8 | "Widen"/"Find new" silently spends credits | Card separates free actions (resume/re-arm) from credit-spending ones (new `DiscoveryJob`), labelled on the action. |

### Components and data flow

```
chatbot agent
  → resolve_enable_context(shop, product_id)        [new, read-only]
       composes get_dynamic_pricing_status + resolve_enable_settings
  → preview_dynamic_pricing_toggle(... , context)    [branch on context.state]
       writes ChatPreview.change shaped per chosen action
  → internal.apply-chat-flag (UI route)              [apply-time re-check + frequency persist]
```

### Changes required

- **New** `services/chatbot_svc/tools/enable_context.py` — the resolver.
- **New** `EnableContext` schema in `services/chatbot_svc/schemas.py`.
- **Edit** `services/chatbot_svc/tools/preview.py` — `preview_dynamic_pricing_toggle`
  branches on `EnableContext.state`; builds the right `change` payload + human copy.
- **Edit** `services/chatbot_svc/prompts/system.md` — instruct the agent to call
  the resolver first and present the state-appropriate card; explain the
  free-vs-paid actions for `PAUSED_WITH_DATA`.
- **Edit** `shopify_ui/app/routes/internal.apply-chat-flag.jsx` — persist
  `frequencyInterval/Unit` on the resume path; add an apply-time state re-check.

### Out of scope

- UI pages (`app.discover.$id.jsx`, products list) — unchanged.
- Gender-gate fix — separate, deferred low-priority item.
- Matcher category/brand gate changes — already done outside this spec.

### Testing

Follow the existing `services/chatbot_svc/tests/` pytest pattern (`conftest.py`
fixtures):

- `resolve_enable_context` returns the right `state` for: no candidates (FRESH),
  candidates present + flag off (PAUSED_WITH_DATA), flag on (ACTIVE).
- `query_drifted` true when latest `DiscoveryJob.query` differs from current.
- `dead_links` counts only DEAD `ProductUrl`s.
- `live_matches` excludes `rejectedByMerchant = true` and WEAK tiers.
- `preview.py` builds the correct `change` payload per chosen action (resume vs
  widen vs new-set) and only the paid actions imply a `DiscoveryJob`.
- apply-route: re-check rejects/adjusts when the flag flipped after preview creation.

---

## Feature 2 — Chatbot as app homepage

### Goal

Land the merchant on the chatbot at `/app`. Move the Products list to
`/app/products`.

### Current state

- `/app` (`app._index.jsx`) = Products list. **Also carries the fresh-install
  product-sync bootstrap** in its loader (ShopifyUser upsert + non-blocking
  background pull when the store was never synced).
- `/app/chatbot` (`app.chatbot.jsx`) = `<ChatPanel />`.
- Nav (`app.jsx`) links: Products → `/app`, Assistant → `/app/chatbot`.

### Changes

1. **Relocate the products list**: rename `app._index.jsx` → `app.products.jsx`
   (route `/app/products`). Component/loader logic unchanged **except** the
   sync-bootstrap block (see decision below).
2. **New `app._index.jsx`**: renders `<ChatPanel />` as the homepage.
3. **Nav (`app.jsx`)**: Products → `/app/products`; the chatbot is now home at
   `/app`. Keep a top-level "Assistant"/home affordance.
4. **`/app/chatbot`**: redirect to `/app` to avoid breaking existing links.

### Sync-bootstrap placement (decided: Option A)

The fresh-install **sync bootstrap** currently fires on Products-list load. With
the chatbot becoming the landing page, the bootstrap moves into the new `/app`
(chatbot) loader so it still fires on first landing. This is the smallest change
and preserves the current "fires on home load" timing. The relocated
`app.products.jsx` loader keeps its read-only product queries but **no longer
owns** the bootstrap.

### Out of scope

- Restyling the chatbot panel itself.
- Any change to chatbot conversation logic beyond Feature 1.

### Testing

- `/app` renders the chatbot; `/app/products` renders the products list.
- `/app/chatbot` redirects to `/app`.
- Fresh-install bootstrap still enqueues a product pull on first `/app` load
  (per chosen option).
- Nav links resolve to the new routes.
