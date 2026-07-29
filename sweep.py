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
from datetime import datetime, timezone

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


def price_key(card_number) -> str | None:
    """'SFD-239/221' -> 'SFD-239'.  'SFD-239*/221' -> 'SFD-239*'.

    card_number arrives as an int for some cards (a bare number with no set
    prefix), so coerce before splitting. A key with no '-' cannot match the
    app's "<set>-<number>" form, so it is dropped rather than stored unjoinable.
    """
    if card_number is None:
        return None
    key = str(card_number).split("/", 1)[0].strip()
    if not key or "-" not in key:
        return None
    return key


def extract(card: dict) -> dict | None:
    """Pull the Cardmarket block down to the five fields the app renders."""
    cm = (card.get("prices") or {}).get("cardmarket")
    if not cm:
        return None
    low = cm.get("lowest_near_mint")
    d7 = cm.get("7d_average")
    d30 = cm.get("30d_average")
    if low is None and d7 is None and d30 is None:
        return None
    out = {"low": low, "d7": d7, "d30": d30, "n": cm.get("available_items")}
    return {k: v for k, v in out.items() if v is not None}


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

    for ep in eps:
        ep_id, ep_name = ep.get("id"), ep.get("name", "?")
        page, pages = 1, 1
        got = seen = 0
        while page <= pages:
            payload = api("cards", episode_id=ep_id, per_page=PER_PAGE, page=page)
            paging = payload.get("paging") or {}
            # The docs promise per_page but not a maximum. Record what the API
            # actually gave us so the log says whether PER_PAGE=100 took effect.
            if honoured_per_page is None:
                honoured_per_page = paging.get("per_page")
            for card in payload.get("data", []):
                seen += 1
                key = price_key(card.get("card_number"))
                if not key:
                    unkeyed += 1
                    continue
                p = extract(card)
                if p is None:
                    unpriced += 1
                    continue
                if key in cards:
                    # Same printed number in two episodes (promos reprint their
                    # base set's number). Keep the first and count it -- the app
                    # refuses to guess for these, same rule as convertCollectionToPromos.
                    collisions += 1
                    continue
                cards[key] = p
                got += 1
            pages = paging.get("total", 1) or 1
            page += 1
        print(f"  {ep_name}: {got} priced of {seen} returned "
              f"({pages} page{'s' if pages != 1 else ''})", file=sys.stderr)

    print(f"per_page honoured by the API: {honoured_per_page} "
          f"(requested {PER_PAGE})", file=sys.stderr)
    if unkeyed:
        print(f"no set-prefixed card_number, skipped: {unkeyed}", file=sys.stderr)
    if unpriced:
        print(f"no cardmarket price block, skipped: {unpriced}", file=sys.stderr)
    if collisions:
        print(f"ambiguous duplicate numbers skipped: {collisions}", file=sys.stderr)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "currency": "EUR",
        "source": "tcggo.com",
        "count": len(cards),
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
