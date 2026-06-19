# Discovery Configuration Consolidation Rollout

## What Changed

This release consolidates discovery configuration to a single source of truth: `ShopifyProduct`. The `DiscoveryJob` table no longer stores configuration—only execution state.

**Dropped fields from `DiscoveryJob`:**
- `numResults` — Merchant's `ShopifyProduct.discoveryNumResults` is now the only source (default: 10)
- `listingExpansionCap` — Merchant's `ShopifyProduct.listingExpansionCap` is now the only source (default: 5)
- `candidateCount` — Queried on-demand from `CompetitorCandidate` relation instead of denormalized field

**Impact:** Internal refactor only. User-facing behavior unchanged. Discovery searches work exactly as before.

## Rollout Checklist

### Pre-Deployment
- [ ] **Backup Production Database** (if applicable)
  ```bash
  # Consult your database backup procedures
  ```

### Database Migration
- [ ] **Run Migration** in production environment:
  ```bash
  cd shopify_ui
  npm run setup  # Runs all pending Prisma migrations
  ```
  Expected output: Migration applied successfully; verify no errors in logs

### Application Restart
- [ ] **Restart Python Workers** to load updated code:
  ```bash
  docker-compose restart
  ```
  Or restart each worker individually:
  ```bash
  # Stop and restart all Celery workers
  ```
  Expected: All workers come back online and begin consuming from queues

- [ ] **Deploy Frontend** to production:
  ```bash
  npm run build
  # Deploy /shopify_ui/build to production
  ```

### Post-Deployment Verification
- [ ] **Smoke Test: Discovery Flow**
  1. Navigate to a product with dynamic pricing enabled
  2. Click "Start Discovery"
  3. Verify: Discovery job appears in UI with correct status
  4. Verify: Candidate count displays correctly once search completes
  5. Verify: Scraped products load without errors
  6. Check browser console: no JavaScript errors

- [ ] **Monitor Worker Logs**
  - Check celery worker logs for errors
  - Verify `discovery-worker`, `scraper-worker`, `extraction-worker` are all healthy
  - Check that discovery jobs move through the pipeline normally

- [ ] **Verify Configuration Resolution**
  - In a test discovery search, confirm that default `discoveryNumResults: 10` is used
  - In a test discovery search, confirm that default `listingExpansionCap: 5` is used
  - Test with overridden values on ShopifyProduct to ensure fallback chain works

## Rollback Plan

If issues arise:

1. **Revert Frontend** to previous deployment
2. **Restart Workers** with previous code:
   ```bash
   git checkout <previous-commit>
   docker-compose restart
   ```
3. **Rollback Database** (if backup exists):
   ```bash
   # Restore from backup; migration is reversible if needed
   cd shopify_ui
   npx prisma migrate resolve --rolled-back <migration-name>
   ```

## Files Modified

**Database:**
- `shopify_ui/prisma/schema.prisma` — Removed 3 fields from DiscoveryJob model
- `shopify_ui/prisma/migrations/` — New migration to drop columns

**Python Services:**
- `services/common/models.py` — Removed Column definitions
- `services/discovery_svc/main.py` — Stopped writing to removed fields
- `services/scraper_svc/celery_beat.py` — Updated query to not read removed fields
- `services/scraper_svc/candidate.py` — Simplified fallback chain for `_resolve_listing_cap()`

**Frontend:**
- `shopify_ui/app/routes/app.discover.$id.jsx` — Query candidateCount on-demand from relation

## No User Impact

- Users do not need to reconfigure discovery settings
- Existing discovery searches continue normally
- UI behavior and appearance unchanged
- All user-facing discovery functionality preserved

## Questions?

Refer to the architecture section in `/CLAUDE.md` for system overview, or contact the development team for deployment support.
