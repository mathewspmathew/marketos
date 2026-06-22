# Pending Clamp Reason Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform technical clamp reason codes on the stats page into detailed, merchant-friendly explanations for pending prices.

**Architecture:** Add a helper function `getClampExplanation()` in the stats page that parses the pricing `reason` field (format: `"ref=X target=Y tier=Z comps=N"`) and generates two-line explanations based on clamp type (`clamped_per_round` or `clamped_lifetime_cap`). Update the JSX rendering to call this helper and display the explanation below the status badge.

**Tech Stack:** React/JSX, regex for string parsing, Shopify Polaris components (`s-stack`, `s-text`)

## Global Constraints

- Single file change: `shopify_ui/app/routes/app.stats.$productId.jsx`
- Frontend-only; no backend, database, or API changes
- Must gracefully fall back to raw reason if parsing fails
- Currency symbol: ₹ (Indian Rupee)
- Decimal format: `.toFixed(2)` (e.g., ₹74.85)
- Only applies to rows where `status === "pending"` AND `clampReason.startsWith("clamped_")`

---

## Task 1: Add Test File for Helper Function

**Files:**
- Create: `shopify_ui/__tests__/routes/app.stats.$productId.test.jsx`

**Interfaces:**
- Produces: Test suite for `getClampExplanation(clampReason, decision)` function

- [ ] **Step 1: Create test file with test imports**

Create file `shopify_ui/__tests__/routes/app.stats.$productId.test.jsx`:

```javascript
import { describe, it, expect } from 'vitest';
import { getClampExplanation } from '../../app/routes/app.stats.$productId.jsx';

describe('getClampExplanation', () => {
  // Tests will be added in steps below
});
```

- [ ] **Step 2: Write test for clamped_per_round**

Add to the test suite:

```javascript
it('returns two-line explanation for clamped_per_round', () => {
  const decision = {
    reason: 'ref=76.38 target=74.85 tier=COMPETITIVE comps=3',
    newPrice: 76.38,
  };
  const result = getClampExplanation('clamped_per_round', decision);
  
  expect(result).not.toBeNull();
  expect(result.line1).toBe('Target ₹74.85 → ₹76.38 (per-round cap)');
  expect(result.line2).toContain('Limited by maximum change per cycle');
  expect(result.line2).toContain('Reference: ₹76.38');
  expect(result.line2).toContain('3 competitors');
  expect(result.line2).toContain('COMPETITIVE');
});
```

- [ ] **Step 3: Write test for clamped_lifetime_cap**

Add to the test suite:

```javascript
it('returns two-line explanation for clamped_lifetime_cap', () => {
  const decision = {
    reason: 'ref=120.00 target=132.00 tier=PREMIUM comps=5',
    newPrice: 110.00,
  };
  const result = getClampExplanation('clamped_lifetime_cap', decision);
  
  expect(result).not.toBeNull();
  expect(result.line1).toBe('Target ₹132.00 → ₹110.00 (lifetime cap)');
  expect(result.line2).toContain('Price adjusted to stay within allowed range');
  expect(result.line2).toContain('Reference: ₹120.00');
  expect(result.line2).toContain('5 competitors');
  expect(result.line2).toContain('PREMIUM');
});
```

- [ ] **Step 4: Write test for null clampReason**

Add to the test suite:

```javascript
it('returns null when clampReason is null', () => {
  const decision = {
    reason: 'ref=76.38 target=74.85 tier=COMPETITIVE comps=3',
    newPrice: 76.38,
  };
  const result = getClampExplanation(null, decision);
  
  expect(result).toBeNull();
});
```

- [ ] **Step 5: Write test for malformed reason string**

Add to the test suite:

```javascript
it('returns null when reason format does not match expected pattern', () => {
  const decision = {
    reason: 'malformed reason string',
    newPrice: 76.38,
  };
  const result = getClampExplanation('clamped_per_round', decision);
  
  expect(result).toBeNull();
});
```

- [ ] **Step 6: Write test for unknown clampReason**

Add to the test suite:

```javascript
it('returns null for unknown clampReason type', () => {
  const decision = {
    reason: 'ref=76.38 target=74.85 tier=COMPETITIVE comps=3',
    newPrice: 76.38,
  };
  const result = getClampExplanation('clamped_unknown', decision);
  
  expect(result).toBeNull();
});
```

- [ ] **Step 7: Run tests to verify they fail**

```bash
cd shopify_ui
npm test -- __tests__/routes/app.stats.$productId.test.jsx
```

Expected output: All 5 tests FAIL with error like "getClampExplanation is not exported"

- [ ] **Step 8: Commit test file**

```bash
git add shopify_ui/__tests__/routes/app.stats.$productId.test.jsx
git commit -m "test: add unit tests for getClampExplanation helper"
```

---

## Task 2: Implement Helper Function

**Files:**
- Modify: `shopify_ui/app/routes/app.stats.$productId.jsx` (add function before component)

**Interfaces:**
- Consumes: `reason` string in format `"ref=X target=Y tier=Z comps=N"`, `clampReason` string, `decision` object with `newPrice` property
- Produces: Function `getClampExplanation(clampReason, decision)` that returns `{line1: string, line2: string}` or `null`

- [ ] **Step 1: Add helper function to stats page**

Open `shopify_ui/app/routes/app.stats.$productId.jsx` and add this function **before the `PriceChart` component definition** (around line 153):

```javascript
/**
 * Parse pricing reason and clamp type to generate merchant-friendly explanation.
 * Returns {line1, line2} or null if reason format doesn't match or clampReason is unknown.
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
  
  if (clampReason === 'clamped_per_round') {
    return {
      line1: `Target ₹${targetPrice.toFixed(2)} → ₹${appliedPrice.toFixed(2)} (per-round cap)`,
      line2: `Limited by maximum change per cycle. Reference: ₹${ref} from ${comps} competitors (${tier} tier).`,
    };
  }
  
  if (clampReason === 'clamped_lifetime_cap') {
    return {
      line1: `Target ₹${targetPrice.toFixed(2)} → ₹${appliedPrice.toFixed(2)} (lifetime cap)`,
      line2: `Price adjusted to stay within allowed range. Reference: ₹${ref} from ${comps} competitors (${tier} tier).`,
    };
  }
  
  return null;
}
```

- [ ] **Step 2: Export the helper function**

Add named export after the function definition:

```javascript
export { getClampExplanation };
```

Actually, to keep it simple and avoid breaking the default export, make it exportable via a re-export at the very end of the file before the default export. Add this line right before the last line (`export default function ProductStatsPage()`):

```javascript
// Export helper for testing
export { getClampExplanation };
```

- [ ] **Step 3: Run tests to verify they pass**

```bash
cd shopify_ui
npm test -- __tests__/routes/app.stats.$productId.test.jsx
```

Expected output: All 5 tests PASS

- [ ] **Step 4: Commit implementation**

```bash
git add shopify_ui/app/routes/app.stats.$productId.jsx
git commit -m "feat: add getClampExplanation helper to generate merchant-friendly clamp messages"
```

---

## Task 3: Update JSX Rendering to Use Helper

**Files:**
- Modify: `shopify_ui/app/routes/app.stats.$productId.jsx` (lines 330–335)

**Interfaces:**
- Consumes: `getClampExplanation(clampReason, decision)` from Task 2
- Produces: Updated JSX that renders friendly explanations instead of raw clampReason

- [ ] **Step 1: Locate current clampReason rendering**

Open `shopify_ui/app/routes/app.stats.$productId.jsx` and find lines 330–335:

```javascript
{d.clampReason && (
  <s-text tone="subdued">{d.clampReason}</s-text>
)}
```

- [ ] **Step 2: Replace with new rendering logic**

Replace those lines with:

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

- [ ] **Step 3: Run the dev server to verify rendering**

```bash
cd shopify_ui
npm run dev
```

Wait for the dev server to start and the tunnel to activate. You should see a message like:
```
✓ Tunnel started
```

- [ ] **Step 4: Navigate to stats page and verify rendering**

1. Open the app in your browser (usually `http://localhost:3000`)
2. Navigate to a product's stats page (e.g., `/app/stats/<productId>`)
3. Find a row with status "Pending push" that has `clampReason` set
4. Verify the explanation displays correctly:
   - Line 1: Shows "Target ₹X → ₹Y (per-round cap)" or "(lifetime cap)"
   - Line 2: Shows context about the constraint and competitors
   - Text is slightly smaller and subdued tone
   - No error in browser console

If no pending+clamp rows exist in your test data, you may need to create a test scenario (see Task 4).

- [ ] **Step 5: Test fallback behavior (malformed reason)**

To verify graceful fallback, you can temporarily add a malformed reason to test data or add a debug console.log. Verify that if reason format doesn't match, the raw clampReason displays (should not crash).

- [ ] **Step 6: Stop dev server**

Press `Ctrl+C` to stop the dev server.

- [ ] **Step 7: Commit JSX changes**

```bash
git add shopify_ui/app/routes/app.stats.$productId.jsx
git commit -m "feat: display merchant-friendly clamp explanations on stats page"
```

---

## Task 4: Manual Verification with Test Data (Optional)

**Files:**
- No files created/modified
- Verification only

**Interfaces:**
- None (testing existing code)

- [ ] **Step 1: Check if test data has pending+clamp scenarios**

Query your development database or test shop to see if there are any `PriceDecision` rows with:
- `autoApplied = true`
- `appliedAt = null` (or recent timestamp, confirming pending)
- `skipReason` like `"clamped_%"`

If you have data, skip to Step 3.

- [ ] **Step 2: Create test scenario (if needed)**

If no test data exists, you can manually insert a test row into the database. Open your DB client and insert:

```sql
INSERT INTO "PriceDecision" (
  id, "shopDomain", "shopifyVariantId",
  "oldPrice", "newPrice", reason,
  "changePct", "refPrice", "formulaTarget",
  "competitorsUsed", "oosObservations", "currencyDrops",
  "topMatchesJson", "tierAtDecision",
  "skipReason", "autoApplied", "decidedAt"
) VALUES (
  gen_random_uuid(),
  'your-test-shop.myshopify.com',  -- your shop domain
  'gid://...',  -- a real variant id from your shop
  100.00, 76.38, 'ref=76.38 target=74.85 tier=COMPETITIVE comps=3',
  NULL, 76.38, 74.85,
  3, 0, 0,
  '[]', 'COMPETITIVE',
  'clamped_per_round', true, NOW()
);
```

- [ ] **Step 3: Start dev server and navigate to stats page**

```bash
cd shopify_ui
npm run dev
```

Navigate to the stats page for the product that has the test decision.

- [ ] **Step 4: Verify the explanation displays**

Look for the pending+clamp row and confirm:
- Status badge shows "Pending push"
- Line 1 shows: "Target ₹74.85 → ₹76.38 (per-round cap)"
- Line 2 shows: "Limited by maximum change per cycle. Reference: ₹76.38 from 3 competitors (COMPETITIVE tier)."
- No console errors

- [ ] **Step 5: Test with lifetime cap scenario**

Update the test row (or insert a new one) with:
```sql
UPDATE "PriceDecision"
SET "skipReason" = 'clamped_lifetime_cap',
    reason = 'ref=120.00 target=132.00 tier=PREMIUM comps=5',
    "newPrice" = 110.00
WHERE id = '<your-test-row-id>';
```

Refresh the page and verify the explanation updates to:
- Line 1: "Target ₹132.00 → ₹110.00 (lifetime cap)"
- Line 2: "Price adjusted to stay within allowed range. Reference: ₹120.00 from 5 competitors (PREMIUM tier)."

- [ ] **Step 6: Stop dev server**

Press `Ctrl+C`.

- [ ] **Step 7: No commit needed**

This task is verification only.

---

## Summary

| Task | Deliverable | Tests |
|------|-------------|-------|
| 1 | Unit tests for `getClampExplanation()` | 5 test cases covering both clamp types, null/malformed inputs |
| 2 | Helper function implementation | All unit tests pass |
| 3 | JSX rendering update | Visual verification on stats page |
| 4 | Manual verification | Clamp messages render correctly for pending prices |

**Total commits:** 3 (tests, implementation, JSX update)

---

## Self-Review Against Spec

✓ **Helper function** — Task 2 implements `getClampExplanation(clampReason, decision)` that parses reason string and returns `{line1, line2}`

✓ **Message format** — Line 1 shows "Target ₹X → ₹Y (constraint type)", Line 2 shows context

✓ **Clamp types covered** — Both `clamped_per_round` and `clamped_lifetime_cap` handled

✓ **Graceful fallback** — Returns `null` if parsing fails; JSX falls back to raw clampReason

✓ **Data extraction** — Regex pattern matches spec's reason format: `ref=X target=Y tier=Z comps=N`

✓ **Conditional rendering** — Only affects rows where `status === "pending"` AND `clampReason.startsWith("clamped_")`

✓ **No backend changes** — Frontend-only; no database or API modifications

✓ **Testing** — Unit tests for logic, manual verification for UI rendering

✓ **Single file** — Only `shopify_ui/app/routes/app.stats.$productId.jsx` modified

✓ **No placeholders** — All code is concrete; no TBD or "add error handling" steps

Plan is solid and ready for execution.
