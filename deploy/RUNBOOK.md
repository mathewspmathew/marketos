# MarketOS Production Deploy Runbook

## Phase 1 — GCP trial VM (now, for submission)

## 1. Provision the GCP VM
1. Start (or confirm) the $300/90-day GCP free trial on the existing GCP
   project — do not create a second account.
2. Note the trial start date; Phase 2 migration must complete by
   day ~80 to leave buffer before the credit expires.
3. Create a Compute Engine `e2-medium` instance (2 vCPU / 4GB, x86_64),
   Ubuntu 22.04, in a region close to your merchants. Attach a public IP.
4. In the VPC firewall rules, leave only SSH (port 22, ideally restricted
   to your IP) open. Do NOT open 80/443 — the Cloudflare Tunnel is
   outbound-only and needs no inbound rule.
5. SSH in, install Docker Engine + the Compose plugin
   (https://docs.docker.com/engine/install/ubuntu/).

## 2. Buy the domain and create the tunnel
1. In the Cloudflare dashboard, register a `.com` via Cloudflare Registrar
   (~$10.44/yr, at-cost, DNS hosted on Cloudflare automatically).
2. In Cloudflare Zero Trust > Networks > Tunnels, create a tunnel named
   `marketos-prod`. Copy the tunnel token.
3. Add a public hostname on the tunnel: your domain -> HTTP ->
   `caddy:80` (the tunnel and Caddy share the compose network once both
   containers are running, so `caddy` resolves by service name).

## 3. Point the Shopify app config at the domain
1. In `shopify_ui/shopify.app.market-pricing.toml`, replace both
   `<YOUR_DOMAIN>` occurrences (`application_url` and the entry in
   `[auth] redirect_urls`) with the real domain purchased in step 2.
2. From your local machine (with the Shopify CLI installed and logged
   into the app's Partner org), run:
   ```bash
   cd shopify_ui && npx shopify app deploy
   ```
   to push the updated config to the Partner Dashboard. This must be
   done before attempting to install the app on any store — Shopify
   validates the install/OAuth redirect against whatever URLs are
   currently registered there.

## 4. Set up GCP credentials on the VM
1. In the existing GCP project (no new trial account), create a service
   account with Vertex AI User + Storage Object Admin roles, scoped to
   only the buckets/models this app uses.
2. Download its JSON key, copy it to the VM at
   `~/.config/gcloud/application_default_credentials.json`
   (`chmod 600`) — this is the path `docker-compose.prod.yml` mounts
   read-only into every service that needs it. (The Phase 1 GCP VM could
   authenticate automatically via its attached service account instead,
   but the explicit key file is used here too so this step is identical
   on both phases — no credential-handling difference to debug when
   migrating to the non-GCP Phase 2 host.)

## 5. Clone the repo onto the VM
```bash
git clone <repo-url> marketos && cd marketos
```
The repo is likely private — make sure an SSH deploy key or a personal
access token is configured on the VM before running this (method left
to you; either works).

## 6. Assemble `.env` on the VM
1. Copy `deploy/.env.prod.example` to `.env` **at the repository root**
   (the same directory as `docker-compose.prod.yml` — i.e.
   `marketos/.env`, not `marketos/deploy/.env`). Compose reads `.env`
   from the directory containing the compose file to resolve every
   `${VAR}` reference in it, so putting it anywhere else means every
   service starts with empty credentials. `chmod 600` it.
2. Fill in every value — pull API keys from wherever they're currently
   stored for local dev, plus:
   - `SHOPIFY_APP_URL=https://<your-domain>`
   - `CLOUDFLARE_TUNNEL_TOKEN=<token from step 2.2>`
3. Confirm `DATABASE_URL` points at the existing Aiven Postgres instance.

## 7. First deploy
```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps   # all services should be "Up"
```

A web-based log viewer (`dozzle`) is also available once the stack is
up, at `127.0.0.1:8888` on the VM — bound to localhost only, by design.
To reach it from your local browser, open an SSH tunnel first:
```bash
ssh -L 8888:127.0.0.1:8888 <user>@<vm-ip>
```
then browse to `http://localhost:8888`.

## 8. Smoke test (before submitting for review)
Run through the full pipeline against the live domain, on a real dev
store:
1. Install the app from `https://<your-domain>` on a Shopify dev store.
2. Add a competitor URL via the app UI; confirm a `ScrapingConfig` row
   appears (check via `docker compose -f docker-compose.prod.yml logs
   scraper-worker`).
3. Wait for a scrape cycle (beat ticks every 30s); confirm a
   `ScrapedProduct` is created and a GCS object exists in
   `GCS_MARKDOWN_BUCKET`.
4. Confirm `ScrapedVariant` rows get `semanticText` and vector columns
   populated (embedding pipeline).
5. Confirm a `ProductMatch` appears for a matching merchant product.
6. Open the Suggestions UI, trigger a re-suggest, confirm a
   `ProductSuggestion` appears with a price band.
7. Approve/apply a suggestion; confirm the Shopify Admin API write-back
   succeeds (check the product's price actually changed in the dev
   store).
8. Uninstall the app; confirm `webhooks/app/uninstalled` fires and any
   cleanup runs (check logs).

Only submit for App Store review once all 8 steps pass against the
live domain — Shopify's reviewer will exercise the same install/use/
uninstall flow manually.

## Phase 2 — Migrate to Hetzner (before day ~80 of the GCP trial)

GCP's permanent free tier (one `e2-micro`, 2 shared vCPU / 1GB RAM) is
too small for this workload, and a paid `e2-small`/`e2-medium` long-term
runs ~$15–24/mo — over the $0–10/mo budget. Migrate before the trial
credit runs out.

## 9. Provision the Hetzner VM
1. Create a Hetzner Cloud account, launch a CX22 instance (2 vCPU / 4GB,
   x86_64, ~€4.50/mo), Ubuntu 22.04.
2. Leave only SSH open in Hetzner's firewall — same no-inbound-80/443
   rule as Phase 1, since the Cloudflare Tunnel is unchanged.
3. SSH in, install Docker Engine + the Compose plugin.
4. Repeat runbook step 4 (GCP credentials) on the new box — same
   service-account key file, same mount path.

## 10. Cut over
1. `git clone` the repo (same private-repo auth caveat as step 5 of
   Phase 1) and copy `.env` onto the Hetzner VM exactly as in step 6 of
   Phase 1 — no `.env` values change; the domain and Cloudflare Tunnel
   token stay the same.
2. `docker compose -f docker-compose.prod.yml up -d --build` on the
   Hetzner VM. The `cloudflared` container starts as part of this and
   self-registers as a new connector for the `marketos-prod` tunnel
   using `CLOUDFLARE_TUNNEL_TOKEN` — no separate `cloudflared service
   install` step is needed (that would create a second, redundant
   host-level connector alongside the container's own).
3. In the Cloudflare Zero Trust dashboard, confirm the new connector
   (from the Hetzner VM's `cloudflared` container) appears in the
   `marketos-prod` tunnel's connector list. Then drain the old
   connector by stopping the stack on the GCP VM:
   ```bash
   docker compose -f docker-compose.prod.yml down
   ```
   The public hostname and DNS record are untouched — only which
   machine answers the tunnel changes, so `application_url` in the
   `.toml` does not need to change.
4. Re-run the 8-step smoke test (runbook section 8) against the live
   domain on the new box before decommissioning the GCP VM.
5. Once confirmed, delete the GCP VM (or let the trial lapse naturally)
   to avoid any unexpected charges once the credit is exhausted.
