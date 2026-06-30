import { describe, it, expect } from 'vitest';
import { getClampExplanation } from '../../app/routes/app.stats.$productId';

describe('getClampExplanation', () => {
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

  it('returns null when clampReason is null', () => {
    const decision = {
      reason: 'ref=76.38 target=74.85 tier=COMPETITIVE comps=3',
      newPrice: 76.38,
    };
    const result = getClampExplanation(null, decision);

    expect(result).toBeNull();
  });

  it('returns null when reason format does not match expected pattern', () => {
    const decision = {
      reason: 'malformed reason string',
      newPrice: 76.38,
    };
    const result = getClampExplanation('clamped_per_round', decision);

    expect(result).toBeNull();
  });

  it('returns null for unknown clampReason type', () => {
    const decision = {
      reason: 'ref=76.38 target=74.85 tier=COMPETITIVE comps=3',
      newPrice: 76.38,
    };
    const result = getClampExplanation('clamped_unknown', decision);

    expect(result).toBeNull();
  });
});
