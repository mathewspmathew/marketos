MarketOS — Project Overview
============================

What it is
----------
MarketOS is an intelligence platform that lives inside a merchant's Shopify
admin. It watches competitor websites, understands what products are being
sold and at what prices, compares those products against the merchant's own
catalog, and then adjusts or recommends smarter prices.

In short: it helps online stores stay competitive automatically, instead of
the owner manually checking rival sites every day.


Who uses it
-----------
- Shopify store owners and their teams.
- They install MarketOS like any other Shopify app and use it from inside
  their normal Shopify dashboard.


How a merchant uses it (the journey)
------------------------------------
1. The merchant switches dynamic pricing on for the products they want to
   compete on. They don't have to hunt for competitors by hand — MarketOS
   finds candidate competitor listings on the web for each chosen product,
   and the merchant simply confirms the ones that are a genuine match.

2. MarketOS visits those confirmed competitor pages in the background, on a
   schedule, and reads them — product names, descriptions, images, prices,
   variants, stock.

3. It then matches each competitor product to the closest product in the
   merchant's own store. For example, "our Blue Running Shoe Size 9" is
   recognised as the same item as a rival's listing, even when the titles
   and photos are different. Strong matches are accepted automatically;
   uncertain matches wait for the merchant to confirm them.

4. For each matched product, MarketOS works out a new price from what
   competitors are charging. When a match is solid and the price move is
   within the guardrails the merchant has set, the new price is pushed to
   Shopify automatically. The merchant can see exactly why a price changed
   and revert it back with one click if they disagree.

5. The merchant reviews the competitive picture on a clean dashboard —
   which products moved, what the market looks like, what changed and why.


The main pages the merchant sees
--------------------------------
- Products   : browse the catalog and switch competitive pricing on or
               off per product.
- Matches    : merchant products paired with their competitor equivalents —
               accept or reject the auto-found candidates, and review the
               ones that aren't a confident match yet.
- Stats      : the competitor price picture per product, price history, and
               the option to revert a price MarketOS changed.
- Assistant  : in-app chat to ask questions about the store and competitors,
               and to take quick actions (toggle dynamic pricing, apply or
               revert a price) with a preview before anything is confirmed.


What happens behind the scenes
------------------------------
The system runs as a set of small background workers, each doing one job:

- A scraper visits competitor pages and pulls the raw content.
- An extractor turns that raw content into clean structured product data
  using an AI model.
- An embedder converts product text and images into a mathematical
  fingerprint so similar products can be compared.
- A matcher uses those fingerprints to pair competitor products with the
  merchant's own products.
- A pricing engine looks at the matches and decides on a price, applying it
  automatically when the match is confident and the change is within the
  merchant's guardrails.
- A writer pushes the new price back to Shopify, and can revert it on
  request.

All of these workers talk to a shared database so the dashboard always
shows the latest picture.


The value in one line
---------------------
MarketOS replaces the slow, manual work of watching competitors and
adjusting prices with an automated loop: watch the market, understand it,
and apply the right move — all from inside Shopify.


Features
=====================

This section describes what is actually working in the product today,
written in plain terms. The pieces are listed in the order a merchant
would encounter them.

Installing and connecting the store
-----------------------------------
A merchant can install MarketOS from Shopify and land inside their admin
on the app's home screen. The app reads the store's products and variants
as soon as it is connected, and keeps them in sync as the merchant
creates, updates or deletes products in Shopify. Each shop has its own
settings area where the merchant controls how MarketOS behaves for them.

Picking which products to compete on
------------------------------------
Not every product needs price tracking. The merchant has a screen where
they can browse their catalog and switch dynamic pricing on or off for
each product. Only products that are switched on are watched and priced
by the system. This keeps attention on the products that actually matter
to the business.

Finding competitors automatically
---------------------------------
Once a product is marked for dynamic pricing, MarketOS goes out and finds
competitor listings for it on the wider web, without the merchant having
to paste links manually. Each suggested competitor product appears as a
candidate the merchant can review — accept the ones that are genuinely the
same thing, reject the ones that are not. Accepted candidates become live
competitor sources that the system keeps watching.

Watching competitor pages
-------------------------
For every accepted competitor URL, MarketOS visits the page on a
schedule, reads it, and pulls out the useful information: product name,
description, images, variants, stock state, and most importantly the
current price. The merchant does not have to do anything for this to
keep happening — it runs quietly in the background.

Matching competitor products to the merchant's own
--------------------------------------------------
The system understands products by their meaning, not just by their
titles, so it can recognize that "Acme Blue Runner, size 9" on a rival's
site is the same item as the merchant's own "Blue Running Shoe (9)".
These pairings show up on a Matches screen. Confident pairings are used
right away; uncertain pairings are flagged for the merchant to confirm or
reject before they count toward pricing.

Understanding the market for each product
-----------------------------------------
For every product with confirmed competitor matches, MarketOS builds a
picture of what the market looks like: the cheapest, the most expensive,
the typical price, and how the merchant's own price sits inside that
range. A stats screen surfaces this per product, and a history view
shows how competitor prices have moved over time.

Deciding and applying prices
-----------------------------
Based on the competitor picture, MarketOS works out a recommended price
for each watched product. When the underlying match is confident enough
and the move stays inside the merchant's configured guardrails, the price
is applied straight to Shopify — no extra click needed. Every applied
price comes with a clear reason on the Stats page, and the merchant can
revert it back to the previous price at any time.

Rules, guardrails and safety
-----------------------------
The merchant is not left guessing what the system will do. They can set
rules — for example, "never go below this floor", "don't change a price
by more than this much in one step" — and these guardrails bound every
price decision the system makes. Matches that aren't confident enough
never reach pricing until the merchant confirms them by hand.

Chat assistant
--------------
MarketOS includes an in-app chat assistant that lets merchants ask
questions about their store and competitors in plain language
("what are my five most undercut products this week?", "show me how this
product's price has moved"). The assistant answers using live data from
the system, and can take quick actions — like flipping dynamic pricing on
or off for a product, or applying/reverting a price — showing the merchant
a preview to confirm before anything changes.
