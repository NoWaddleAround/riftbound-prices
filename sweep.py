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

PER_PAGE = 100          # only honoured when episode_id is set; 20 otherwise
CALL_CEILING = 80       # of the 100/day, leaving headroom for retries + manual
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


def price_key(card_number: str | None) -> str | None:
    """'SFD-239/221' -> 'SFD-239'.  'SFD-239*/221' -> 'SFD-239*'."""
    if not card_number:
        return None
    return card_number.split("/", 1)[0].strip() or None


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
    eps = api("episodes", per_page=PER_PAGE).get("data", [])
    if not eps:
        raise SystemExit("no episodes returned -- check BASE_URL and the game slug")
    print(f"episodes: {len(eps)}", file=sys.stderr)

    cards: dict[str, dict] = {}
    collisions = 0

    for ep in eps:
        ep_id, ep_name = ep.get("id"), ep.get("name", "?")
        page, total = 1, 1
        got = 0
        while page <= total:
            payload = api("cards", episode_id=ep_id, per_page=PER_PAGE, page=page)
            for card in payload.get("data", []):
                key = price_key(card.get("card_number"))
                if not key:
                    continue
                p = extract(card)
                if p is None:
                    continue
                if key in cards:
                    # Same printed number in two episodes (promos reprint their
                    # base set's number). Keep the first and count it -- the app
                    # refuses to guess for these, same rule as convertCollectionToPromos.
                    collisions += 1
                    continue
                cards[key] = p
                got += 1
            total = (payload.get("paging") or {}).get("total", 1) or 1
            page += 1
        print(f"  {ep_name}: {got} priced", file=sys.stderr)

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
    if snapshot["count"] < 500:
        raise SystemExit(f"only {snapshot['count']} priced cards -- refusing to publish")


if __name__ == "__main__":
    main()
