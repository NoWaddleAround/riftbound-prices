#!/usr/bin/env python3
"""Daily Riftbound price snapshot, built from Cardmarket's public Data Tables exports.

Replaces the TCGGO/RapidAPI sweep. That version was metered at 100 calls/day and covered 719 of
the app's 1376 printings; this one is unmetered and covers ~1275, because it reads Cardmarket's
own catalogue rather than a third party's view of it.

THREE PUBLIC INPUTS, no key and no auth:
    productCatalog/productList/products_singles_22.json   catalogue: idProduct, name, expansion
    productCatalog/priceGuide/price_guide_22.json         prices, keyed by idProduct
    (products_nonsingles_22.json is sealed product, not used)

TWO LOCAL INPUTS, copied from the app (both already ship inside the public APK):
    cards.json             the 1376 printings that need pricing
    cardmarket_links.json  1193 exact V1/V2/V3 slugs -- how a showcase is told from its base

🪤 The catalogue has NO collector number. Every printing of a card is a separate idProduct with
the SAME name, so "Diana, Scorn of the Moon" appears three times. The V-number in the app's own
Cardmarket slug is what separates them, and ascending idProduct within a name group tracks
V1 -> V2 -> V3. Without that, a showcase would silently inherit its base card's price.

Usage:
    python sweep.py                 # fetch, join, write out/prices.json
    python sweep.py --local DIR     # use already-downloaded files in DIR (offline testing)
    python sweep.py --force         # rebuild even if Cardmarket's data hasn't changed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

BASE = "https://downloads.s3.cardmarket.com/productCatalog"
SINGLES_URL = f"{BASE}/productList/products_singles_22.json"
GUIDE_URL = f"{BASE}/priceGuide/price_guide_22.json"

# Where the previous snapshot is read from, for carry-forward and the no-change check.
PUBLISHED_URL = os.environ.get(
    "PUBLISHED_URL", "https://nowaddlearound.github.io/riftbound-prices/prices.json")

TIMEOUT = 60
MIN_CARDS = 900          # refuse to publish a snapshot smaller than this
MAX_CARRY_DAYS = 90      # how long a delisted card keeps its last known price

# idExpansion -> the app's set code. Derived from products_nonsingles_22.json, which names the
# sealed product of each wave ("Spiritforged Nexus Night Promo Booster" sits in 6483 -> SFDX).
# ⚠️ A new set means a new id here, or its cards silently go unpriced. The run log prints any
# expansion it does not recognise, so a missing one is loud rather than invisible.
EXPANSION = {
    6286: "OGN", 6289: "OGS", 6399: "SFD", 6491: "UNL", 6587: "VEN",
    6322: "OGNX", 6483: "SFDX", 6567: "UNLX", 6588: "VENX", 6480: "PROK",
}

# Price fields in preference order.
#
# `low` FIRST, and the app says so out loud: "Lowest on CM regarding any language". Cardmarket
# treats language as a property of a LISTING, not a separate product, so one idProduct pools
# English, Chinese and everything else -- and on Riftbound the cheapest copy is very often a
# Chinese printing, which is why this reads well under the English market. That is not a defect
# to hide, it is what the number is, and the label is what makes it honest.
#
# ⚠️ **Keep this field and that label in step.** Showing `avg1` (a sale average) under a heading
# that says "lowest" would be a lie with a decimal point on it.
#
# 🪤 The fallbacks are rare by design: `low` covers 97.1% of products. avg1/avg7/avg30 sit at
# 68.5% -- they are SALE averages, absent for any card nobody traded -- so they cannot lead.
# `src` records which field answered, so a mislabelled tail is at least detectable.
PRICE_FIELDS = ("low", "trend", "avg1", "avg7", "avg30")


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "riftbound-prices/2.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def previous_snapshot() -> dict:
    """The last published snapshot, for carry-forward and the unchanged check."""
    try:
        req = urllib.request.Request(
            PUBLISHED_URL, headers={"User-Agent": "riftbound-prices/2.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"no previous snapshot ({e})", file=sys.stderr)
        return {}


def norm(s: str) -> str:
    """Loose name key, so 'Diana, Scorn of the Moon' == 'Diana-Scorn-of-the-Moon'."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def squash(s: str) -> str:
    """Name key with every separator removed, not turned into a space.

    🪤 [norm] turns an apostrophe into a SPACE, so Cardmarket's "Mel, Soul's Reflection" becomes
    'mel soul s reflection' while its own slug Mel-Souls-Reflection becomes 'mel souls reflection'
    — the same card, two keys, and VEN-151/195 went unpriced for it. Dropping separators rather
    than spacing them makes both 'melsoulsreflection'. Used only as a fallback, after norm.
    """
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# 🪤 Ascending idProduct does NOT always track V1 -> V2. Cardmarket created every SFD/UNL rune's
# SHOWCASE product days BEFORE its common one, so the group order is reversed for those twelve
# cards and the version in the slug indexes the wrong half. Swain, Visionary 173 is here for a
# different reason: Cardmarket left its second product un-versioned (bare slug), so there is no
# -V2 for the link to carry and it would read as V1 forever.
#
# Verified card by card against the product pages on 2026-07-31. Checked and NOT needed anywhere
# else: all 79 versioned Vendetta products and UNL Baron Nashor V1/V2/V3 order correctly by id.
# Regenerate with the riftbound app repo's scratchpad/cardmarket_variant_links.py notes.
def load_overrides(assets: str) -> dict:
    path = os.path.join(assets, "rune_pid_overrides.json")
    try:
        return json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        print("no rune_pid_overrides.json — 25 printings will take the wrong product's price",
              file=sys.stderr)
        return {}


def app_name_variants(name: str):
    """Names to try for one app card, most specific first.

    🪤 The app decorates some names in ways Cardmarket does not: OGS starters carry a
    ' - Starter' suffix and the three OGN Recruits carry a domain tag '(DE)'/'(NX)'/'(ZN)'.
    Those are the bulk of the cards that failed to match on a first pass -- our naming, not
    Cardmarket's data.
    """
    yield norm(name)
    stripped = re.sub(r"\s*-\s*Starter$", "", name)
    stripped = re.sub(r"\s*\([A-Z]{2,3}\)$", "", stripped)
    if stripped != name:
        yield norm(stripped)


def price_of(row: dict | None):
    """Best available figure and which field it came from."""
    if not row:
        return None, None
    for field in PRICE_FIELDS:
        v = row.get(field)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v), field
    return None, None


def build(singles: list, guide: dict, cards: list, links: dict,
          overrides: dict | None = None) -> tuple[dict, dict]:
    overrides = overrides or {}
    # (normalised name, set code) -> [idProduct...] ascending; ascending id tracks V1 -> V2 -> V3,
    # EXCEPT where `overrides` says otherwise — see its note.
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    squashed: dict[tuple[str, str], list[int]] = defaultdict(list)
    unknown_expansions: dict[int, int] = defaultdict(int)
    for p in singles:
        code = EXPANSION.get(p["idExpansion"])
        if code is None:
            unknown_expansions[p["idExpansion"]] += 1
            continue
        groups[(norm(p["name"]), code)].append(p["idProduct"])
        squashed[(squash(p["name"]), code)].append(p["idProduct"])
    for v in groups.values():
        v.sort()
    for v in squashed.values():
        v.sort()

    out: dict[str, dict] = {}
    stats = defaultdict(int)
    per_set: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    unmatched: list[str] = []

    for c in cards:
        code, number, name = c["set"], c["number"], c["name"]
        per_set[code][0] += 1
        key = f"{code}-{number}"

        # A printing whose product the group order cannot express. Pinned, never guessed.
        forced = overrides.get(f"{name}|{code}|{number}")
        if forced is not None:
            value, field = price_of(guide.get(forced))
            if value is None:
                stats["matched but unpriced"] += 1
                continue
            entry = {"low": round(value, 2), "src": field, "pid": forced}
            g = guide.get(forced) or {}
            foil = g.get("trend-foil") or g.get("low-foil")
            if isinstance(foil, (int, float)) and not isinstance(foil, bool) and foil > 0 \
                    and abs(foil - value) > 0.005:
                entry["foil"] = round(float(foil), 2)
            out[key] = entry
            stats[f"priced via override ({field})"] += 1
            per_set[code][1] += 1
            continue

        # The app's own Cardmarket link, when it has one, names the exact variant.
        url = links.get(f"{name}|{code}|{number}")
        slug_name, variant = None, None
        if url:
            m = re.search(r"/Singles/[^/]+/([^?]+)", url)
            if m:
                slug = m.group(1)
                vm = re.search(r"-V(\d+)", slug)
                variant = int(vm.group(1)) if vm else 1
                slug_name = norm((slug[:vm.start()] if vm else slug).replace("-", " "))

        group = None
        candidates = ([slug_name] if slug_name else []) + list(app_name_variants(name))
        for candidate in candidates:
            group = groups.get((candidate, code))
            if group:
                break
        if not group:
            # Separator-insensitive second pass: catches the apostrophe cases norm() splits.
            for candidate in candidates:
                group = squashed.get((squash(candidate), code))
                if group:
                    break

        if not group:
            stats["no product for this name"] += 1
            unmatched.append(f"{code}-{number} {name}")
            continue

        if variant is not None and variant - 1 < len(group):
            pid, how = group[variant - 1], "slug"
        elif len(group) == 1:
            pid, how = group[0], "unique"
        else:
            # Several printings share this name and nothing says which is which. Refusing to
            # guess is the same rule convertCollectionToPromos follows (handoff §4 Home).
            stats["ambiguous, no slug"] += 1
            unmatched.append(f"{code}-{number} {name} ({len(group)} candidates)")
            continue

        value, field = price_of(guide.get(pid))
        if value is None:
            stats["matched but unpriced"] += 1
            continue

        entry = {"low": round(value, 2), "src": field, "pid": pid}
        # Foil columns mostly echo `low`; carry one only when it genuinely differs, so the app
        # can price a foil ghost properly instead of falling back to its base card.
        g = guide.get(pid) or {}
        foil = g.get("trend-foil") or g.get("low-foil")
        if isinstance(foil, (int, float)) and not isinstance(foil, bool) and foil > 0 \
                and abs(foil - value) > 0.005:
            entry["foil"] = round(float(foil), 2)

        out[key] = entry
        stats[f"priced via {how} ({field})"] += 1
        per_set[code][1] += 1

    if unknown_expansions:
        print("⚠️  UNKNOWN EXPANSIONS — their cards cannot be priced until EXPANSION learns them:",
              file=sys.stderr)
        for exp, n in sorted(unknown_expansions.items(), key=lambda x: -x[1]):
            print(f"     idExpansion {exp}: {n} singles", file=sys.stderr)

    print("--- join ---", file=sys.stderr)
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {v:>5}  {k}", file=sys.stderr)
    print("--- per set ---", file=sys.stderr)
    for code in sorted(per_set):
        tot, got = per_set[code]
        print(f"  {code:<6} {got:>4}/{tot:<4} {100 * got / tot:5.1f}%", file=sys.stderr)
    if unmatched:
        print(f"--- unmatched ({len(unmatched)}) ---", file=sys.stderr)
        for u in unmatched[:25]:
            print(f"     {u}", file=sys.stderr)

    return out, dict(stats)


SOURCE = "cardmarket-data-tables"


def carry_forward(cards: dict, previous: dict, today: str) -> tuple[int, int]:
    """Keep the last known price for a card that has dropped out of Cardmarket's catalogue.

    Rare now that the source is the catalogue itself, but a delisted product would otherwise
    read as 0.00€ in the app and drop a collection total by its full value overnight.

    🪤 **Never carry across a change of source.** The previous snapshot may have been built from
    TCGGO, whose figures are `lowest_near_mint` — a different measure entirely. Importing those
    under a label that reads "Lowest on CM regarding any language" would put a wrong number
    behind a confident sentence, which is worse than showing nothing for those cards.
    """
    if previous.get("source") != SOURCE:
        if previous:
            print(f"previous snapshot came from {previous.get('source', 'an older build')!r} — "
                  "not carrying its prices across the source change", file=sys.stderr)
        return 0, 0
    carried = expired = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_CARRY_DAYS)).strftime("%Y-%m-%d")
    for key, prev in (previous.get("cards") or {}).items():
        if key in cards:
            continue
        value = prev.get("low")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            continue
        since = prev.get("since") if prev.get("src") == "carried" else today
        if since < cutoff:
            expired += 1
            continue
        cards[key] = {"low": float(value), "src": "carried", "since": since}
        carried += 1
    return carried, expired


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/prices.json")
    ap.add_argument("--local", help="read the Cardmarket files from this directory instead")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even when Cardmarket's data has not changed")
    ap.add_argument("--assets", default=".", help="where cards.json / cardmarket_links.json live")
    args = ap.parse_args()

    if args.local:
        singles_doc = json.load(
            open(os.path.join(args.local, "products_singles_22.json"), encoding="utf-8"))
        guide_doc = json.load(
            open(os.path.join(args.local, "price_guide_22.json"), encoding="utf-8"))
    else:
        singles_doc = fetch(SINGLES_URL)
        guide_doc = fetch(GUIDE_URL)

    guide_created = guide_doc.get("createdAt", "")
    print(f"catalogue createdAt : {singles_doc.get('createdAt')}", file=sys.stderr)
    print(f"price guide createdAt: {guide_created}", file=sys.stderr)

    previous = previous_snapshot()
    # ⚠️ The real freshness signal, not the clock. Cardmarket regenerates the price guide around
    # 02:45 Berlin but the exact minute drifts, so the workflow runs a little after and this
    # decides whether there is anything new to publish. Beats guessing a schedule.
    if not args.force and previous.get("source_created_at") == guide_created:
        print("Cardmarket data unchanged since the last snapshot — nothing to publish.",
              file=sys.stderr)
        # Exit 100, not 0: the workflow reads this as "skip the deploy" and leaves the live
        # snapshot alone. Exiting 0 would have it publish an out/ directory that was never
        # written. This is also what makes the two DST crons safe — whichever one runs before
        # Cardmarket's nightly drop simply lands here.
        raise SystemExit(100)

    cards = json.load(open(os.path.join(args.assets, "cards.json"), encoding="utf-8"))
    links = json.load(open(os.path.join(args.assets, "cardmarket_links.json"), encoding="utf-8"))

    guide = {p["idProduct"]: p for p in guide_doc["priceGuides"]}
    built, _ = build(singles_doc["products"], guide, cards, links, load_overrides(args.assets))

    now = datetime.now(timezone.utc)
    live = len(built)
    carried, expired = carry_forward(built, previous, now.strftime("%Y-%m-%d"))
    if carried:
        print(f"carried forward: {carried}", file=sys.stderr)
    if expired:
        print(f"expired after {MAX_CARRY_DAYS} days: {expired}", file=sys.stderr)

    snapshot = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SOURCE,
        "source_created_at": guide_created,
        "currency": "EUR",
        "count": len(built),
        "live": live,
        "carried": carried,
        "cards": built,
    }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, separators=(",", ":"), ensure_ascii=False)

    size = os.path.getsize(args.out)
    print(f"\nwrote {args.out}: {len(built)} cards, {size / 1024:.1f} KB", file=sys.stderr)

    # A collapse means the catalogue changed shape or EXPANSION went stale. Failing loudly beats
    # publishing a thin snapshot over a good one.
    if len(built) < MIN_CARDS:
        raise SystemExit(f"only {len(built)} priced (floor {MIN_CARDS}) — refusing to publish")


if __name__ == "__main__":
    main()
