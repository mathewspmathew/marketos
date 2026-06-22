# Pending Clamp Reason Messages Design

**Date:** 2026-06-22  
**Status:** Approved  
**Scope:** Stats page pending+clamp reason messaging

## Overview

When a price decision is pending (enqueued to Shopify but not yet applied) AND the target price was clamped by per-round or lifetime constraints, the stats page currently shows the technical code (`clamped_per_round`, `clamped_lifetime_cap`).

This design transforms those codes into detailed, merchant-friendly explanations that clarify:
- What the constraint was (per-round change limit vs. lifetime floor/ceiling)
- What the target price would have been
- What it was clamped to
- The competitive context (reference price, number of competitors, tier)

## User Experience

### Before
```
Status: Pending push
clampReason: clamped_per_round
```
Merchant is left wondering: "Why is my price pending? What's holding it back?"

### After
```
Status: Pending push

Target ₹74.85 → ₹76.38 (per-round cap)
Limited by maximum 5% change per cycle. Reference: ₹76.38 
from 3 competitors (COMPETITIVE tier).
```
Merchant understands immediately: the algorithm wanted to lower the price, but the per-round safety limit is preventing a large jump in one cycle.

## Data Flow

### Backend (No Changes)
Python `pricing_svc/decide.py` already generates and stores:
- `reason` field: `"ref=76.38 target=74.85 tier=COMPETITIVE comps=3"`
- `skipReason` field: `"clamped_per_round"` or `"clamped_lifetime_cap"`

### Stats Page Loader (No Changes)
`shopify_ui/app/routes/app.stats.$productId.jsx` already fetches and returns:
```javascript
{
  oldPrice: 76.38,        // current price before decision
  newPrice: 76.38,        // price after clamping
  refPrice: 76.38,        // reference price from competitors
  reason: "ref=76.38 target=74.85 tier=COMPETITIVE comps=3",
  clampReason: "clamped_per_round",
  status: "pending"
}
```

### Frontend Display (New Logic)
When rendering a stats row:
1. Check if `status === "pending"` AND `clampReason` starts with `"clamped_"`
2. Parse the `reason` string using regex: `/ref=([\d.]+)\s+target=([\d.]+)\s+tier=(\w+)\s+comps=(\d+)/`
3. Extract: `ref`, `target`, `tier`, `comps`
4. Generate two-line explanation based on clamp type
5. Display below the "Pending push" badge

## Implementation Details

### Helper Function

Create a new function in `app.stats.$productId.jsx`:

```javascript
/**
 * Parse pricing reason and clamp type to generate merchant-friendly explanation.
 * Returns {line1, line2} or null if reason format doesn't match.
 */
function getClampExplanation(clampReason, decision) {
  if (!clampReason) return null;
  
  // Parse reason string: "ref=X target=Y tier=Z comps=N"
  const reasonMatch = decision.reason.match(
    /ref=([\d.]+)\s+target=([\d.]+)\s+tier=(\w+)\s+comps=(\d+)/
  );
  if (!reasonMatch) return null;
  
  const [, ref, target, tier, comps] = reasonMatch;
  const targetPrice = parseFloat(target);
  const appliedPrice = decision.newPrice;
  
  if (clampReason === "clamped_per_round") {
    return {
      line1: `Target ₹${targetPrice.toFixed(2)} → ₹${appliedPrice.toFixed(2)} (per-round cap)`,
      line2: `Limited by maximum change per cycle. Reference: ₹${ref} from ${comps} competitors (${tier} tier).`,
    };
  }
  
  if (clampReason === "clamped_lifetime_cap") {
    return {
      line1: `Target ₹${targetPrice.toFixed(2)} → ₹${appliedPrice.toFixed(2)} (lifetime cap)`,
      line2: `Price adjusted to stay within allowed range. Reference: ₹${ref} from ${comps} competitors (${tier} tier).`,
    };
  }
  
  return null;
}
```

### JSX Rendering Update

Replace lines 330–335 in the status section of the table row with:

```javascript
{d.clampReason && (
  (() => {
    const explanation = getClampExplanation(d.clampReason, d);
    if (!explanation) return d.clampReason;
    return (
      <s-stack direction="block" gap="tight">
        <s-text tone="subdued">{explanation.line1}</s-text>
        <s-text tone="subdued" size="small">{explanation.line2}</s-text>
      </s-stack>
    );
  })()
)}
```

### File Change Summary
- **File:** `shopify_ui/app/routes/app.stats.$productId.jsx`
- **Changes:**
  - Add `getClampExplanation()` helper function (before the component)
  - Update clampReason rendering in the table row (lines 330–335)

## Examples

### Scenario 1: Per-Round Cap

**Context:**
- Current price: ₹100.00
- Reference price: ₹76.38 (from 3 competitors)
- Target formula: 5% markup on ref → ₹80.20
- Per-round limit: 5% → max ₹5 change allowed
- Decision: Move from ₹100 → ₹95 (limited from desired ₹80.20)

**Display:**
```
Status: Pending push

Target ₹80.20 → ₹95.00 (per-round cap)
Limited by maximum change per cycle. Reference: ₹76.38 
from 3 competitors (COMPETITIVE tier).
```

**Merchant reads:** "I want to move closer to competitor prices, but the system only allows 5% moves at a time to avoid customer shock. It'll get there gradually."

### Scenario 2: Lifetime Cap (Ceiling)

**Context:**
- Current price: ₹100.00
- Reference price: ₹120.00 (from 3 competitors)
- Target formula: 10% uplift → ₹132.00
- Max price allowed: ₹110.00 (100 + 10% lifetime cap)
- Decision: Hold at ₹110.00 (clamped from desired ₹132.00)

**Display:**
```
Status: Pending push

Target ₹132.00 → ₹110.00 (lifetime cap)
Price adjusted to stay within allowed range. Reference: ₹120.00 
from 3 competitors (PREMIUM tier).
```

**Merchant reads:** "The algorithm wants to price higher, but I've set a maximum limit. It respects that boundary."

### Scenario 3: Lifetime Cap (Floor)

**Context:**
- Current price: ₹100.00
- Reference price: ₹50.00 (from 3 competitors)
- Target formula: 10% undercut → ₹45.00
- Min price allowed: ₹85.00 (100 - 15% lifetime cap)
- Decision: Hold at ₹85.00 (clamped from desired ₹45.00)

**Display:**
```
Status: Pending push

Target ₹45.00 → ₹85.00 (lifetime cap)
Price adjusted to stay within allowed range. Reference: ₹50.00 
from 3 competitors (BUDGET tier).
```

**Merchant reads:** "I'm trying to undercut competitors aggressively, but I've set a floor to protect margin. The system respects it."

## Scope & Constraints

### In Scope
- Transform `clamped_per_round` and `clamped_lifetime_cap` messages on the stats page
- Only affects rows where `status === "pending"` AND `clampReason.startsWith("clamped_")`
- Purely frontend transformation — no DB or backend changes
- Parsing is defensive (gracefully falls back to raw reason if format doesn't match)

### Out of Scope
- Changes to other status types (applied, failed, skipped)
- Changes to the pricing decision logic itself
- New fields or columns in the database
- Backend message generation

### Backward Compatibility
- No database schema changes
- No API response changes
- Graceful fallback if reason format is unexpected
- Works with all existing pricing tiers and constraint types

## Testing Approach

### Unit Testing (Frontend)
- Test `getClampExplanation()` with valid reason formats
- Test fallback when reason format doesn't match
- Test both `clamped_per_round` and `clamped_lifetime_cap`

### Integration Testing
- Render stats page with pending+clamp decisions
- Verify messages appear correctly
- Verify parsing extracts correct values
- Test with various price points and tiers

### Manual Testing
- View stats page for a product with pending+clamp history
- Verify merchant can understand why price is held
- Check formatting (₹ symbol, decimals, tier names)

## Success Criteria

✓ Merchant looking at a pending+clamp row immediately understands:
  - What the target price wanted to be
  - What it's actually being set to
  - Why it's constrained (per-round cap vs lifetime cap)
  - Competitive context (ref price, # competitors, tier)

✓ No technical jargon (no "clamped_per_round", no "skipReason")

✓ Consistent formatting across all clamp scenarios

✓ No performance impact (parsing is in-table, not in loader)

## Future Considerations

- Could extend to explain other skipReasons (e.g., "no_change", "below_min_competitors") with similar friendly messages
- Could add a help tooltip explaining what "per-round cap" means in merchant terms
- Could surface the actual cap percentages if stored in ShopSettings (currently not in stats response)
