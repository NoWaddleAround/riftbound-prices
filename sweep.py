#!/usr/bin/env python3
"""Daily Riftbound price snapshot via the RapidAPI "riftbound-prices-api" listing.

Writes prices.json keyed by "<SET>-<NUMBER>" -- the API's card_number with the
"/<set total>" suffix stripped. That key is exactly what the app can compose
from its own CardSeed fields (set + number), so no mapping table is needed for
the four base sets.

TCGGO_API_KEY holds the RapidAPI key (the secret name is kept for continuity).

Budget: the free tier allows 100 calls/day. A full sweep is ~16-21 calls.
CALL_CEILING is a hard stop so a pagination bug can never drain the day's quota.

Usage:
    python sweep.py --probe     # 1 call, verifies auth + shape, writes nothing
    python sweep.py             # full sweep -> prices.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# The API is reached through the RapidAPI gateway, not tcggo.com directly.
# The listing is Riftbound-specific, so there is no /{game} path segment:
# it is /cards, not /riftbound/cards.
RAPIDAPI_HOST = os.environ.get(
    "RAPIDAPI_HOST", "riftbound-prices-api.p.rapidapi.com")
BASE_URL = f"https://{RAPIDAPI_HOST}"

PER_PAGE = 100          # the docs promise per_page but state no maximum;
                        # the run log reports what the API actually honoured
CALL_CEILING = 80       # of the 100/day, leaving headroom for retries + manual
MIN_CARDS = 200         # refuse to publish a snapshot smaller than this
MAX_CARRY_DAYS = 90     # how long a sold-out card keeps its last known price

# Where the previous snapshot is read from for carry-forward. Not an API call.
PUBLISHED_URL = os.environ.get(
    "PUBLISHED_URL", "https://nowaddlearound.github.io/riftbound-prices/prices.json")
TIMEOUT = 30
RETRY_STATUS = {429, 500, 502, 503, 504}

_calls = 0
_remaining = None       # RapidAPI's own quota counter, read off each response


class BudgetExceeded(RuntimeError):
    pass


def api(path: str, **params) -> dict:
    """One GET against the API. Counts against the daily budget."""
    global _calls, _remaining
    if _calls >= CALL_CEILING:
        raise BudgetExceeded(f"hit the {CALL_CEILING}-call ceiling")

    headers = {
        "x-rapidapi-key": os.environ["TCGGO_API_KEY"],
        "x-rapidapi-host": RAPIDAPI_HOST,
        "Accept": "application/json",
    }

    url = f"{BASE_URL}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    # Two attempts only -- a retry costs a call from the same budget.
    for attempt in range(2):
        _calls += 1
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                left = resp.headers.get("x-ratelimit-requests-remaining")
                if left is not None:
                    _remaining = left
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in RETRY_STATUS and attempt == 0:
                time.sleep(5)
                continue
            body = e.read()[:400].decode("utf-8", "replace")
            hint = ""
            if e.code in (401, 403):
                hint = ("\nhint: check the TCGGO_API_KEY secret holds your "
                        "RapidAPI key, and that you are subscribed to the listing.")
            elif e.code == 429:
                hint = "\nhint: daily quota exhausted -- it resets on RapidAPI's clock."
            raise SystemExit(f"HTTP {e.code} on {url}\n{body}{hint}") from e
    raise SystemExit(f"gave up on {url}")


# The app's own code for each promo wave, by the API's episode name.
#
# 🪤 "Origins: Promos" reports code "OGN" -- the BASE set's code. Keying its 96 promos with that
# would collide with every ordinary Origins card and silently poison real prices. The app calls
# that wave OGNX (handoff §6), and this map is the only place that correction lives.
#
# An episode not listed here falls back to its own `code`. That is why Project K Promos keys as
# PROK: the app has no such set, so those 5 cards simply never match -- honest, and harmless.
PROMO_SET_CODE = {
    "Origins: Promos": "OGNX",
    "Spiritforged: Promos": "SFDX",
    "Unleashed: Promos": "UNLX",
    "Vendetta: Promos": "VENX",
}


def price_key(card_number, episode_code: str | None = None) -> str | None:
    """'SFD-239/221' -> 'SFD-239'.  'SFD-239*/221' -> 'SFD-239*'.  '007' -> 'SFDX-007'.

    card_number arrives as an int for some cards, so coerce before splitting.

    🪤 **Promo episodes number their cards WITHOUT a set prefix** -- bare "007", "125", "FND251".
    Nothing in the number itself says which wave it belongs to, so the episode's code has to
    supply it. Before this, every one of those returned None and ~170 promos were discarded as
    unkeyable, which read in the app as "the API has no promos" rather than "we threw them away".
    """
    if card_number is None:
        return None
    key = str(card_number).split("/", 1)[0].strip()
    if not key:
        return None
    if "-" in key:
        return key
    return f"{episode_code}-{key}" if episode_code else None


def _positive(cm: dict, field: str) -> float | None:
    """A Cardmarket figure, or None when it is missing, null, non-numeric or <= 0."""
    v = cm.get(field)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if v > 0 else None


def extract(card: dict) -> dict | None:
    """Pull the Cardmarket block down to what the app renders.

    🪤 **`low` is a best-available figure, not strictly the lowest listing.** When every copy
    sells out, Cardmarket has no "lowest near mint" to report -- but the 7- and 30-day averages
    survive, because they describe trades that already happened. Reading only `lowest_near_mint`
    would price a sold-out chase card at nothing the morning after it sold, which is the one
    number it certainly is not worth. `src` records when a fallback was used.
    """
    cm = (card.get("prices") or {}).get("cardmarket")
    if not cm:
        return None

    low = _positive(cm, "lowest_near_mint")
    d7 = _positive(cm, "7d_average")
    d30 = _positive(cm, "30d_average")

    best = low or d7 or d30
    if best is None:
        return None

    out: dict = {"low": best}
    if low is None:
        out["src"] = "d7" if d7 else "d30"
    if d7 is not None:
        out["d7"] = d7
    if d30 is not None:
        out["d30"] = d30
    n = cm.get("available_items")
    if isinstance(n, int):
        out["n"] = n
    return out


def previous_snapshot() -> dict:
    """The last published snapshot, for carry-forward.

    ⚠️ This is a plain GET of our own Pages URL -- **it is not an API call and costs nothing
    against the 100/day budget.** Deliberately not routed through api(), which counts.

    Returns {} on the very first run, or if Pages is briefly unavailable. That degrades to the
    old behaviour (no carry-forward) rather than failing the sweep.
    """
    try:
        req = urllib.request.Request(
            PUBLISHED_URL, headers={"User-Agent": "riftbound-prices-sweep"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")).get("cards") or {}
    except Exception as e:  # noqa: BLE001 -- any failure means "no previous snapshot"
        print(f"no previous snapshot to carry forward ({e})", file=sys.stderr)
        return {}


def carry_forward(cards: dict, previous: dict, today: str) -> tuple[int, int]:
    """Reinstate cards that had a price yesterday and have none today.

    A card falls out of the sweep when nothing is listed AND no average survives -- the exact
    situation after the last copy of a chase card sells. Emitting nothing for it makes the app
    render 0.00€, so a collection total would drop by the card's full value overnight.

    Entries are stamped with `since`, the date the price stopped being live, and dropped once
    that is older than MAX_CARRY_DAYS: a price nobody has confirmed in three months has stopped
    being information.
    """
    carried = expired = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_CARRY_DAYS)).strftime("%Y-%m-%d")
    for key, prev in previous.items():
        if key in cards:
            continue
        value = prev.get("low")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            continue
        # Keep the ORIGINAL since-date across repeated carries, or the age never advances.
        since = prev.get("since") if prev.get("src") == "carried" else today
        if since < cutoff:
            expired += 1
            continue
        cards[key] = {"low": float(value), "src": "carried", "since": since}
        carried += 1
    return carried, expired


def sweep() -> dict:
    # /episodes documents only search + page, so don't send per_page here.
    eps = api("episodes").get("data", [])
    if not eps:
        raise SystemExit("no episodes returned -- check the host and the key")
    print(f"episodes: {len(eps)}", file=sys.stderr)

    cards: dict[str, dict] = {}
    collisions = 0
    unkeyed = 0
    unpriced = 0
    honoured_per_page = None
    # Per-episode census, published INSIDE the snapshot rather than only logged. The log scrolls
    # away and needs someone to copy it; this is fetchable from the Pages URL forever, and it is
    # what answers "does the API even carry this set / these promos".
    episodes: list[dict] = []

    for ep in eps:
        ep_id, ep_name = ep.get("id"), ep.get("name", "?")
        # What a bare, unprefixed number in this episode should be keyed as.
        ep_code = PROMO_SET_CODE.get(ep_name) or ep.get("code")
        page, pages = 1, 1
        got = seen = 0
        dupes = 0
        samples: list[str] = []
        while page <= pages:
            payload = api("cards", episode_id=ep_id, per_page=PER_PAGE, page=page)
            paging = payload.get("paging") or {}
            # The docs promise per_page but not a maximum. Record what the API
            # actually gave us so the log says whether PER_PAGE=100 took effect.
            if honoured_per_page is None:
                honoured_per_page = paging.get("per_page")
            for card in payload.get("data", []):
                seen += 1
                # Eight, not three: the promo waves number their cards in ways that only become
                # readable across a handful of examples (sequential? base-set numbers? letters?).
                if len(samples) < 8:
                    samples.append(str(card.get("card_number")))
                key = price_key(card.get("card_number"), ep_code)
                if not key:
                    unkeyed += 1
                    continue
                p = extract(card)
                if p is None:
                    unpriced += 1
                    continue
                if key in cards:
                    # Same printed number in two episodes: promos reprint their base set's
                    # number, so a Nexus-Night Poro and the ordinary one are both "OGN-210".
                    # Keeping the first is the same refusal-to-guess as convertCollectionToPromos.
                    # 🪤 A high `dupes` on an episode is the signature of a PROMO WAVE -- that is
                    # the number to read when deciding whether promo pricing is reachable at all.
                    collisions += 1
                    dupes += 1
                    continue
                cards[key] = p
                got += 1
            pages = paging.get("total", 1) or 1
            page += 1

        episodes.append({
            "id": ep_id,
            "name": ep_name,
            "slug": ep.get("slug"),
            "code": ep.get("code"),
            "key_prefix": ep_code,
            "released_at": ep.get("released_at"),
            "returned": seen,
            "priced": got,
            "duplicate_numbers": dupes,
            "sample_numbers": samples,
        })
        print(f"  {ep_name}: {got} priced of {seen} returned "
              f"({pages} page{'s' if pages != 1 else ''}"
              f"{f', {dupes} duplicate numbers' if dupes else ''})", file=sys.stderr)

    print(f"per_page honoured by the API: {honoured_per_page} "
          f"(requested {PER_PAGE})", file=sys.stderr)
    if unkeyed:
        print(f"no set-prefixed card_number, skipped: {unkeyed}", file=sys.stderr)
    if unpriced:
        print(f"no cardmarket price block, skipped: {unpriced}", file=sys.stderr)
    if collisions:
        print(f"ambiguous duplicate numbers skipped: {collisions}", file=sys.stderr)

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    live = len(cards)

    # Anything priced yesterday and missing today keeps its last known figure. Without this a
    # sold-out card reads 0.00€ in the app and a collection total drops by its full value.
    carried, expired = carry_forward(cards, previous_snapshot(), today)
    if carried:
        print(f"carried forward (nothing listed today): {carried}", file=sys.stderr)
    if expired:
        print(f"dropped, unlisted over {MAX_CARRY_DAYS} days: {expired}", file=sys.stderr)

    fallbacks = sum(1 for v in cards.values() if v.get("src") in ("d7", "d30"))
    if fallbacks:
        print(f"priced from an average, nothing listed: {fallbacks}", file=sys.stderr)

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "currency": "EUR",
        "source": "tcggo.com",
        "count": len(cards),
        "live": live,
        "carried": carried,
        "episodes": episodes,
        "cards": cards,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="spend exactly 1 call to verify auth and response shape")
    ap.add_argument("-o", "--out", default="out/prices.json")
    args = ap.parse_args()

    if not os.environ.get("TCGGO_API_KEY"):
        raise SystemExit("TCGGO_API_KEY is missing or empty -- check the repo secret")

    if args.probe:
        payload = api("cards", per_page=1)
        print(json.dumps(payload, indent=2)[:2000])
        print(f"\n-- calls used: {_calls}, quota left today: {_remaining}",
              file=sys.stderr)
        return

    snapshot = sweep()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, separators=(",", ":"), ensure_ascii=False)

    size = os.path.getsize(args.out)
    print(f"wrote {args.out}: {snapshot['count']} cards, {size/1024:.1f} KB, "
          f"{_calls} calls used, quota left today: {_remaining}", file=sys.stderr)

    # A sweep that collapses to almost nothing means the API changed shape.
    # Fail loudly rather than publishing an empty snapshot over a good one.
    # Deliberately low until a first full run establishes the real total --
    # raise it to roughly 80% of that number once you know it.
    if snapshot["count"] < MIN_CARDS:
        raise SystemExit(f"only {snapshot['count']} priced cards "
                         f"(floor is {MIN_CARDS}) -- refusing to publish")


if __name__ == "__main__":
    main()
