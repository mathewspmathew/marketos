-- Per-product knob: how many competitor links discovery should pull each run.
ALTER TABLE "ShopifyProduct"
  ADD COLUMN "discoveryNumResults" INTEGER NOT NULL DEFAULT 10;
