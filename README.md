# Drop these files into NoWaddleAround/riftbound-prices

## The short version

1. Put `cardmarket_links.json`, `sweep.py`, `rune_pid_overrides.json` in the repo **root**, and
   `prices.yml` in `.github/workflows/`. Commit.
2. GitHub → **Actions** → *Daily price snapshot* → **Run workflow**, tick **force**.
3. In the app: **Settings → Refresh prices now**.

Right now step 2 would publish even without `force` (Cardmarket's stamp is 2026-07-31, the live
snapshot's is 2026-07-29). `prices.yml` adds the checkbox anyway, because the moment a daily cron
publishes with the old links the manual re-run no-ops for the rest of that day — see below.


The app's Cardmarket links were rebuilt on 2026-07-31 (handoff §8 batch (t)). That file is also
what `sweep.py` reads to pick a card's `idProduct`, so the prices repo has to move with it —
**the app alone cannot fix a price.** Every figure in the app comes from the published
`prices.json`, and that file is only rewritten when this repo's workflow runs.

Copy all three into the repo root, replacing what is there. `cards.json` is already identical
(sha256 `11c69f9140…`) and does not need touching.

| file | goes | why |
|---|---|---|
| `cardmarket_links.json` | root | 83 links rebuilt — the `-V1-`/`-V2-`/`-V3-` slugs are what name the variant |
| `sweep.py` | root | two fixes, below |
| `rune_pid_overrides.json` | root | new input `sweep.py` reads |
| `prices.yml` | `.github/workflows/` | adds a `force` checkbox to the manual run |

🪤 **The unchanged-data check cannot see that our own inputs changed.** `sweep.py` exits **100**
when Cardmarket's `createdAt` matches the published `source_created_at`, and the workflow reads
that as "skip the deploy". Edit the links, the overrides or `sweep.py` on a day the cron has
already published and every re-run that day no-ops — the fix sits in the repo, unpublished, and
the app keeps showing the old prices. That is why `prices.yml` gains the `force` input.

⚠️ The app caches `prices.json` and only refetches on a schedule, so a device already holding the
old snapshot will not update the moment this publishes. Settings → Refresh, or wait for the daily
gate (`PriceStore.isDue`).

## What changed in sweep.py

**1. `PID_OVERRIDES` (via `rune_pid_overrides.json`), 25 printings.** The header comment claims
"ascending idProduct tracks V1 -> V2 -> V3". That is false for all twelve Spiritforged/Unleashed
rune groups — Cardmarket created each *showcase* product days **before** its common one, so the
version in the slug indexes the wrong half of the group:

| | idProduct ascending | what they actually are |
|---|---|---|
| SFD Fury Rune | 871893, 872478 | 871893 = **V2 showcase** (3,48 €) · 872478 = V1 common (0,02 €) |
| UNL Chaos Rune | 885242, 888474 | 885242 = **V2 showcase** (5,50 €) · 888474 = V1 common (0,02 €) |

Until the links carried a `-V<n>` both rune printings read as V1 and got the showcase price, so the
showcase was accidentally right. With correct links they would have **swapped** — this override is
what stops that. `Swain, Visionary|VEN|173` is in the same file for a different reason: Cardmarket
left its second product un-versioned, so there is no `-V2` for the link to carry.

Every entry was read off its own Cardmarket product page on 2026-07-31. Checked and **not** needed
anywhere else: all 79 versioned Vendetta products and UNL Baron Nashor V1/V2/V3 order correctly.

**2. `squash()` fallback.** `norm()` turns an apostrophe into a space, so Cardmarket's
`Mel, Soul's Reflection` became `mel soul s reflection` while its own slug `Mel-Souls-Reflection`
became `mel souls reflection` — never matched, so VEN-151/195 were unpriced. A separator-free
second pass matches both as `melsoulsreflection`. Runs only after `norm()` fails.

## Verified locally against Cardmarket's live data tables

1276 → **1299** cards priced. **No card loses a price.** Per set: SFD 300/300, VEN 227/228,
UNL 298/300, OGN 349/352.

| printing | published now | after |
|---|---|---|
| `VEN-185` Kayle, Justified (showcase) | 0,43 € | **79,00 €** |
| `VEN-197` Kennen, Heart of the Tempest (showcase) | 0,02 € | **160,00 €** |
| `VEN-169` Zed, From the Shadows (showcase) | 3,00 € | **79,00 €** |
| `VEN-173` Swain, Visionary (showcase) | 0,18 € | **69,00 €** |
| `VEN-195` Mel, Soul's Reflection (showcase) | *unpriced* | **130,00 €** |
| `UNL-238` Baron Nashor (showcase) | 15,95 € | **1.900,00 €** |
| `SFD-R01` Fury Rune (common) | 3,48 € | **0,02 €** |
| `SFD-R01a` Fury Rune (showcase) | 3,48 € | 3,48 € (already right, stays right) |

Each figure matches the "From" price on that product's own Cardmarket page.
