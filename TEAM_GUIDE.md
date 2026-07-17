# SNL Dental Solutions: The Zoho to Shopify Pipeline

**Team guide and living reference.** Last updated: 17 July 2026.
This document is maintained automatically: when the daily maintenance run learns something new, it appends a dated entry to the Findings Log at the bottom. If something here contradicts what you see in Zoho, tell Leke; one of them is wrong and it matters which.

---

## 1. The one rule everything depends on

**Zoho is the source of truth. The Shopify store follows it automatically.**

You never edit the store directly. You keep Zoho accurate, and every morning at 07:07 an automated maintenance run reads Zoho and updates the store: prices, stock, photos, product copy, markdowns on expiring goods. If Zoho is right, the store is right by the next morning. If Zoho is wrong, the store will be wrong by the next morning, and it will stay wrong until Zoho is fixed.

This is why the team's Zoho habits matter so much. The automation is thorough, but it cannot know that a price was typed with a missing zero. It will publish exactly what Zoho says.

## 2. What happens automatically every day

Two routines keep the store correct. Catalogue looks after what products are; Commerce looks after what they cost and how many are shown. Nobody on the team triggers any of this.

### Every morning: Catalogue (07:07)

In order:

| Step | What it does | What you see |
|---|---|---|
| Image Queue | Downloads the image URLs you pasted in the "DS - Zoho - Shopify - Fix" sheet, checks quality, reviews each image against the product, uploads the good ones | Row status changes to Fetched or URL Rejected with a reason |
| Auto image search | Tries to find product photos for items still missing images, using manufacturer sites first | Items move forward, or get added to the Image Queue for you |
| Image audit | Every fetched image is visually reviewed before it can go anywhere near the store. Wrong images are deleted and the item goes back in the queue | Rejection reasons in the daily report |
| Change watch | Finds every item edited in Zoho since yesterday. If the photo or the product copy changed on a live item, it is flagged and re-pushed to the store the same run | Your Zoho edits appear on the store next morning |
| Sync | Publishes queued items to the store: creates products, groups variants, uploads images, sets prices and stock | New products go live |
| Verify | Checks the store to confirm each published item is actually live, with image, description, and correct price | Failures are flagged, never silently ignored |
| Reconcile | Compares every live item against Zoho: price, stock, expiry markdowns. Corrects the store where it drifted. Pulls expired goods off the store | Price and stock changes land; expiring goods get discounted automatically |
| Gallery | Publishes the extra product photos you added via the Image 2 URL and Image 3 URL fields, after visual review | Products get multi-photo galleries |
| Fix Tracker | Works through every Open and Reopened row in the Issues tab | Status, resolution notes, and date filled in |
| Report | Summarises everything above, including anything that needs a human decision | Daily summary for Leke |

### Every afternoon: Commerce (13:07 and 13:42)

| Time | What it does | What you see |
|---|---|---|
| 13:07 The midday check | Runs on its own, even with no computer open. Corrects store prices and stock against Zoho, applies expiry markdowns and short-dated tags, pulls expired goods off the store | A price you fix in Zoho before one o'clock is on the store the same afternoon |
| 13:42 The Commerce review | Reads what the morning run and the midday check did. Reports price and stock drift, expiry buckets, batch caps, and items ready for cutover. If the midday check itself failed to run, the review notices and re-runs it | Afternoon summary for Leke when something needs attention |

**The safety ceiling.** If more than 50 price and stock corrections are ever pending at once, the automation stops and reports instead of fixing. A pile that big means something upstream broke, and pushing it to the store would spread the damage.

## 3. How an item travels the pipeline

Every item carries a **Shopify status** field in Zoho that shows exactly where it is:

1. **Queue for Enrichment.** Selected for the store. The system writes the customer-facing title, description, grouping, and tags.
2. **Enrichment Complete, then Image Validated or Image required.** The item's photo is checked (sharp, square, at least 800 px, shows the exact product). No usable photo means the item waits at Image required until auto-search or the Image Queue solves it.
3. **Queue for Upload.** Copy and image both approved. Ready to publish.
4. **Published.** Live on the store. From here on, the change watch and the reconciler keep it correct forever; nobody needs to touch it again unless something about the product genuinely changes.
5. **Update Required.** A live item whose photo or copy changed in Zoho. The next sync pushes the change and sets it back to Published. The change watch sets this automatically; you can also set it by hand to force a re-push.
6. **Needs Review or Error uploading.** Something needs a human. The reason is always written in the Sync result and Shopify sync notes fields on the item.

**Grouping happens automatically.** Same manufacturer plus same product family becomes one store product with variants (sizes, shades, ISO codes). You do not need to create variants in Zoho, and you should not: see the rules in section 5.

## 4. Your tasks, and why each one is necessary

### Daily habits inside Zoho

| Task | Why it matters |
|---|---|
| Keep selling prices correct | The reconciler pushes Zoho prices to the store every morning. Zoho's own Shopify connection never syncs prices, so this pipeline is the only thing keeping store prices honest |
| Keep stock movements recorded | Store stock follows Zoho stock. Wrong stock sells goods you do not have |
| Replace bad item photos directly in Zoho | The change watch notices the new photo by content, not by name, and re-publishes it the same morning. Always a square-ish, clean photo of the exact product, at least 800 px |
| Write plain descriptions when you know the product | Anything you improve in Zoho copy reaches the store automatically |

### The Fix Tracker sheet (Issues tab)

When you see anything wrong on the live store (wrong image, wrong price, bad description, missing product), log one row: date, SKU, issue type, and what you saw. That is the entire job.

Why it is necessary: the automation cannot see the store the way a customer does. Your row triggers a diagnosis the next morning, the fix is applied, and the resolution notes tell you exactly what was done and what to check. Every fix also teaches the system a lesson (a blocked image source, a corrected description pattern) so the same class of mistake becomes rarer over time.

Statuses you will see the system write: **In Progress**, **Fixed** (with notes and date), **Needs Manual** (the system could not solve it and explains what it needs from you). If you check the store and a "Fixed" item is still wrong, set the row back to **Reopened**: the system treats that as "the first approach failed" and is required to try a different strategy, never the same one twice.

### The Image Queue tab (same sheet)

Items land here when safe automatic image search failed: niche lab items, colour-specific variants, apparel, own-brand goods. For each row, find the exact product image (the suggested source column tells you where to look) and paste the URL in column F.

Why it is necessary: image search engines return plausible-looking wrong products, and a wrong photo in dental supply destroys trust instantly. Roughly 9 in 10 automatic finds for these niche items were wrong when audited. A person who knows the stock finds the right image in seconds. Every URL you provide also teaches the system a trusted source for that brand.

What makes a good URL: manufacturer or major distributor page, the exact model, colour, shade, and pack size, no watermarks, no reseller banners, product only. If no image exists online (SNL brochures, unbranded stock), a clean photo of the physical item works: neutral background, straight on, good light.

### The four custom fields (new, on every item form)

| Field | Who fills it | When |
|---|---|---|
| **Is Batch Item** | Team | Tick once on every product that expires (composites, cements, anaesthetics, sealers, gloves, and similar). This single tick switches on the entire expiry automation for that item |
| **Expiry Date** | Team | On tagged items: the nearest expiry date of the stock currently on the shelf. Update it when new stock arrives or old stock sells out |
| **Image 2 URL / Image 3 URL** | Team | Optional. Paste manufacturer photo URLs on products that deserve a multi-photo gallery. They are reviewed and published the next morning |
| **Batch Migrated** | System only | Bookkeeping for the batch-tracking transition. Please do not edit |

### The rules (things that break the automation)

- **Do not use "Contains Variants" when creating items.** Create every item as a Single Item with its own SKU. The pipeline builds store variants automatically and more safely. Variant items created without SKUs are invisible to every part of the automation. (The test group "PERM ABUT (3MM)" in Zoho, four variants with no SKUs and zero prices, is exactly the situation to avoid.)
- **Never change an item's SKU or name on a live item** outside the batch cutover procedure below. SKU is the thread connecting Zoho, the store, and order history.
- **Prices under ₦100 never publish.** The pipeline treats them as placeholders and skips them.
- **No em dashes in product text.** Store copy uses commas, parentheses, and the word "to" for ranges. The system enforces and auto-corrects this, so mostly just know it exists.

## 5. The expiration workflow (our next major push)

### Why

Some of our goods expire. Selling expired dental materials is a compliance problem, and letting short-dated stock sit at full price until it expires is a financial one. The new workflow discounts short-dated stock automatically and pulls expired stock off the store before a customer can order it.

### What happens automatically once an item is tagged

Tag **Is Batch Item** and fill **Expiry Date**, and every morning the reconciler applies this policy:

| Days until expiry | Store price |
|---|---|
| More than 180 | Full price |
| 91 to 180 | 10% off, full price shown struck through |
| 31 to 90 | 25% off |
| 30 or fewer | 40% off |
| Expired | Product pulled off the store the same morning, listed for disposal in the daily report |

The discount never goes below the item's purchase cost; items that hit that floor are flagged as disposal candidates. Zoho's selling price is never modified: discounts exist only on the store, so your price records stay clean.

### The transition: from date-on-a-field to true batch tracking

Right now expiry lives in one field per item, which the team updates by hand. The destination is Zoho's built-in batch tracking, where every stock receipt carries its own batch number and expiry date and the system always knows the soonest one. The catch, confirmed by testing: **Zoho cannot switch an existing item to batch tracking once it has any transaction history.** So the transition is a rolling replacement, item by item, with zero effect on the store:

- **Phase 0 (now).** Tag expiring items and fill Expiry Date. Markdown automation is live immediately, before any migration.
- **Phase 1 (immediately, new items only).** Any new expiring product is created with batch tracking switched on from day one. From then on, expiry data enters automatically when stock is received. No dates to maintain by hand.
- **Phase 2 (rolling cutovers, triggered by reordering).** When a tagged item's stock drops to its reorder level, the daily report lists it as **cutover ready**: the shelf is at its lowest right before a resupply, so it is the easiest moment to count. The steps, per item:
  1. Team counts the remaining units per expiry date, then renames the old item's name and SKU with a `-X` suffix (two fields, one edit, in Zoho). This frees the identity.
  2. The automation creates the replacement: same SKU and name as before, batch tracking on, the counted units as its opening batches, all store fields copied, marked Published, old item deactivated and flagged Batch Migrated.
  3. The team receives the new delivery into the rebuilt item, entering batch number and expiry at the receiving step. Every reorder migrates one more item.
  4. **The store never notices.** Same product page, same variant, same order history.
- **Phase 3.** When every expiring item is batch tracked, the hand-maintained Expiry Date field retires.

**Several batches on the shelf:** the store sells one batch at a time, oldest first. The listing shows only the soonest batch's quantity, at that batch's discounted price, and carries a "short-dated" product tag. Fresh stock stays hidden until the old batch sells out; the next morning the listing flips to the fresh batch at its own price and the tag comes off. Nobody ever buys fresh stock at the old batch's discount. When shipping a discounted order, pick the short-dated batch: it is what the customer paid for. A tiny discounted batch that lingers unsold is flagged in the daily report (sell offline to unblock the fresh stock). Expired units on a mixed shelf never pull the listing while fresh batches remain; they are flagged for removal from sellable stock. A listing only comes off the store when everything on the shelf is expired.

### How this ties into the rest of the pipeline

The expiry watch is not a separate system. It lives inside the same morning reconciler that fixes prices and stock, reads the same Zoho fields, and reports in the same daily summary. Tagging an item connects it to everything at once: markdowns, expiry buckets in the report (30, 60, 90 days), the expired pull, and the cutover queue.

## 6. How the whole thing connects

```
TEAM (Zoho + the Fix sheet)                 AUTOMATION (07:07 and 13:07)            STORE
────────────────────────────               ─────────────────────────────          ──────────
create Single Items  ──────────────────►   select, enrich, group, image  ──────►  new products
fix prices / stock / photos  ──────────►   change watch + reconciler  ─────────►  corrections land
tag Is Batch Item + Expiry Date  ──────►   expiry watch  ────────────────────►    markdowns, expired pulled
paste Image Queue / gallery URLs  ─────►   review + publish images  ──────────►   photos and galleries
log store problems in Issues tab  ─────►   diagnose, fix, learn lesson  ───────►  problem gone next day
rename -X when cutover ready  ─────────►   batch-tracked replacement  ─────────►  store unaffected
```

Everything the team does is an input in Zoho or the Fix sheet. Everything on the store is an output. Nothing requires touching Shopify.

## 7. Current state (17 July 2026)

- 989 items live on the store; price and stock drift: **zero** after the first reconciliation corrected 17 stale prices and 24 stock counts
- Store copy cleaned: 112 titles, 44 option values, and 21 descriptions rewritten to the new copy standard in one pass, both in Zoho and on the store
- Known debts being worked through the daily report: 49 items published in Zoho but missing on the store, 46 legacy store products with no Zoho item, about 90 Image Queue rows waiting for URLs
- Expiry workflow: built and armed; waiting on the first wave of Is Batch Item tags from the team
- Maintenance is now two named routines: Catalogue (07:07, what products are) and Commerce (13:07 check plus 13:42 review, what products cost and how many are shown); the division of work with Zoho's own Shopify connection is agreed and written up in the Findings Log below

## 8. Findings Log

New entries are appended here by the daily maintenance run when something worth the team's attention is learned.

- **2026-07-17.** The automation is now two named routines. Catalogue (07:07) looks after what products are: photos, text, publishing, the Fix sheet. Commerce (13:07 and 13:42) looks after what products cost and how many are shown: prices, stock, expiry discounts. Commerce also checks its own health: if the midday check ever fails silently, the afternoon review notices and re-runs it.
- **2026-07-17.** Safety ceiling added to the automatic corrections: if more than 50 price and stock fixes are ever pending at once, the automation stops and reports instead of fixing. A pile that big means something upstream broke, and pushing it to the store would spread the damage.
- **2026-07-17.** The automation now runs twice a day. The 07:07 morning run handles everything in section 2; a lighter automatic check at 13:07 corrects prices and stock and applies expiry markdowns, with nobody at the computer. Fix a price in Zoho before one o'clock and the store has it the same afternoon.
- **2026-07-17.** Plan agreed for how our automation and Zoho's own Shopify connection share the work. The connection keeps bringing store orders and customers into Zoho (roughly every 4 hours), and it keeps matching new products behind the scenes each night. Our automation owns everything the store displays: products, photos, text, prices, discounts and the quantities shown. The day the team tags the first "Is Batch Item", two things happen together: the automatic checks move to hourly, and the connection's stock push is switched off, so a batch discount can never appear with the wrong quantity behind it. Until then nothing changes.
- **2026-07-17.** Mixed-batch selling solved: the store sells one batch at a time. A listing with short-dated stock shows only that batch's quantity at its discounted price (plus a "short-dated" tag); fresh stock stays hidden until the old batch sells out, then the listing flips the next morning. Shopify was researched and confirmed to have no native expiry handling (apps would mean a second hand-maintained batch ledger), so this lives in our own automation.
- **2026-07-17.** Expiry workflow refined for real shelves: expired units on a mixed shelf never pull the listing while fresh stock remains (flagged for disposal instead). Cutover timing changed from "stock hits zero" to "just before a resupply arrives" (flagged at reorder level). FAQ added to the guide's Expiring Goods page.
- **2026-07-17.** Pipeline hardening completed: exact product matching (prevents variants attaching to the wrong product), automatic daily reconciliation of prices and stock (Zoho's native Shopify connection was confirmed to never sync prices), change watch for photo and copy edits on live items, expiry markdown automation, multi-photo galleries, and the store-wide copy cleanup. The four new custom fields went live on the item form.
- **2026-07-17.** Confirmed by testing: existing Zoho items with any transaction history can never be switched to batch tracking, and item SKUs and names stay reserved even after deactivation. Both facts shaped the cutover procedure in section 5.
- **2026-07-16.** Image audit of one day's automatic fetches: 2 of 27 were correct. Generic disposables (mixing pads, cups, tray covers) and apparel are now permanently routed to the Image Queue for team-sourced URLs instead of wasting search quota.
