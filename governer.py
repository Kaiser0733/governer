#!/usr/bin/env python3
"""governer — hard spend governor for AI agents.

Budget alerts are not kill switches. governer prices every request the way
the provider actually bills it (cache reads and writes included), then stops
the agent at the line you set.

Zero dependencies. One file. Python 3.9+.

  governer prices [--upcoming] [--json]
  governer box    [--limit 10] [--model gpt-6-astra] [--context 200000]
  governer check  usage.json [--limit 200]
  governer verify ledger.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES_PATH = os.environ.get("GOVERNER_PRICES", os.path.join(HERE, "prices.json"))

# Provider logs spell the same field three ways. Accept all of them.
ALIASES = {
    "input": ("input", "input_tokens", "prompt_tokens"),
    "output": ("output", "output_tokens", "completion_tokens"),
    "cache_read": ("cache_read", "cache_read_input_tokens", "cached_tokens"),
    "cache_write": ("cache_write", "cache_creation_input_tokens"),
}


def load_prices(path=PRICES_PATH):
    with open(path) as f:
        return json.load(f)["models"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- money math

def price_of(model, prices):
    try:
        return prices[model]
    except KeyError:
        raise SystemExit(f"unknown model: {model}\nknown models: {', '.join(sorted(prices))}")


def unit_cost(model, prices):
    """USD per token: (input, output, cache_read, cache_write) + derived flag."""
    p = price_of(model, prices)
    derived = False
    cr, cw = p.get("cache_read"), p.get("cache_write")
    if cr is None:  # ponytail: vendor convention (10%) when unverified, flagged in output
        cr, derived = p["in"] * 0.1, True
    if cw is None:
        cw, derived = p["in"] * 1.25, True
    return (p["in"] / 1e6, p["out"] / 1e6, cr / 1e6, cw / 1e6), derived


def normalize(record):
    """Map provider field spellings onto our four counters."""
    out = {}
    for key, names in ALIASES.items():
        for n in names:
            if n in record:
                out[key] = record[n]
                break
    return out


def cost(model, usage, prices):
    (i, o, cr, cw), _ = unit_cost(model, prices)
    return (usage.get("input", 0) * i + usage.get("output", 0) * o
            + usage.get("cache_read", 0) * cr + usage.get("cache_write", 0) * cw)


def sanity(model, usage, prices):
    """Warnings for what the number might be missing."""
    out, p = [], price_of(model, prices)
    above = p.get("tiered_above")
    if above and usage.get("input", 0) + usage.get("cache_read", 0) > above:
        out.append(f"above {above:,}-token tier boundary — cost shown is a lower bound")
    if p.get("peak_pricing"):
        out.append("vendor bills peak/off-peak — cost shown assumes peak")
    return out


# ---------------------------------------------------------------- the breaker

class Tripped(Exception):
    def __init__(self, spent, limit, would_be):
        self.spent, self.limit, self.would_be = spent, limit, would_be
        super().__init__(
            f"tripped at ${spent:.2f} of ${limit:.2f}; "
            f"request blocked (would have cost ${would_be - spent:.4f})")


def _hash(rec):
    body = json.dumps({k: v for k, v in rec.items() if k != "hash"}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


class Wallet:
    """Append-only, hash-chained spend ledger with a hard limit.

    charge() raises Tripped *before* the money is spent — that is the whole
    point. An alert tells you after; this refuses.
    """

    def __init__(self, limit, path=None):
        self.limit = float(limit)
        self.spent = 0.0
        self.path = path
        self.prev = "genesis"
        if path and os.path.exists(path):
            for rec in read_ledger(path):
                self.spent += rec["cost"]
                self.prev = rec["hash"]

    def charge(self, amount, **meta):
        if self.spent + amount > self.limit:
            raise Tripped(self.spent, self.limit, self.spent + amount)
        self.spent += amount
        rec = {"ts": now(), "cost": round(amount, 8), "total": round(self.spent, 8),
               "prev": self.prev, **meta}
        rec["hash"] = _hash(rec)
        self.prev = rec["hash"]
        if self.path:
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec


def read_ledger(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def verify_ledger(path):
    prev, n, total = "genesis", 0, 0.0
    for rec in read_ledger(path):
        if rec.get("prev") != prev:
            return False, n, f"chain broken at record {n + 1} (prev hash mismatch)"
        if rec.get("hash") != _hash(rec):
            return False, n, f"record {n + 1} was edited after it was written"
        prev, n, total = rec["hash"], n + 1, total + rec.get("cost", 0)
    return True, n, f"{n} records · ${total:.2f} · chain intact"


# ---------------------------------------------------------------- commands

def cmd_prices(args):
    prices = load_prices()
    if args.upcoming:
        rows = [(m, c) for m, p in prices.items() for c in p.get("changes", [])]
        if not rows:
            print("no announced price changes in the dataset")
            return
        print(f"{'model':<22} {'when':<12} {'now':<16} {'after':<16} change")
        for m, c in rows:
            p = prices[m]
            after = f"${c['in']}/${c['out']}"
            change = f"+{(c['in'] / p['in'] - 1) * 100:.0f}%"
            print(f"{m:<22} {c.get('date') or 'TBA':<12} "
                  f"${p['in']}/{p['out']:<10} {after:<16} {change}  {c.get('why', '')}")
        return
    if args.json:
        print(json.dumps(prices, indent=2, sort_keys=True))
        return
    print(f"{'model':<22} {'in/M':>8} {'out/M':>8} {'cache r':>8} {'cache w':>8}  verified")
    for m, p in sorted(prices.items()):
        (_, _, cr, cw), derived = unit_cost(m, prices)
        flag = "~" if derived else " "
        print(f"{m:<22} {p['in']:>8.2f} {p['out']:>8.2f} "
              f"{cr * 1e6:>7.2f}{flag} {cw * 1e6:>7.2f}{flag}  {p.get('verified', '?')}")
    print("\n~ = cache price from vendor convention (0.1x read, 1.25x write), not confirmed")


def cmd_box(args):
    prices = load_prices()
    if args.model not in prices:
        raise SystemExit(f"unknown model: {args.model}")
    w = Wallet(args.limit, args.ledger)
    paid = tin = tout = turns = 0.0
    hit_cache = not args.cache_miss
    print(f"wallet ${args.limit:.2f} · {args.model} · {args.context:,} tok/turn · "
          f"{'cache working' if hit_cache else 'CACHE MISSING'}")
    print()
    try:
        while turns < args.max_turns:
            turns += 1
            usage = {"input": 0 if hit_cache else args.context,
                     "cache_read": args.context if hit_cache else 0,
                     "output": 500}
            c = cost(args.model, usage, prices)
            w.charge(c, turn=int(turns), model=args.model, usage=usage)
            paid += c
            tin += usage["input"] + usage["cache_read"]
            tout += usage["output"]
            if turns <= args.max_turns - 1:
                print(f"  turn {int(turns):>4}   ${w.spent:>8.2f} / ${args.limit:.2f}")
    except Tripped as t:
        print(f"\n  turn {int(turns):>4}   BLOCKED  (would have cost "
              f"${t.would_be - t.spent:.4f})")
        print(f"\n🛑 BREAKER TRIPPED at ${t.spent:.2f} of ${t.limit:.2f} — agent stopped")
    else:
        print(f"\nno trip: ${w.spent:.2f} of ${args.limit:.2f} after {int(turns)} turns")

    (_, _, cr, _), _ = unit_cost(args.model, prices)
    if_cache = tin * cr + tout * price_of(args.model, prices)["out"] / 1e6
    print(f"\n   paid              ${paid:>10.2f}  ({int(turns)} turns)")
    print(f"   same run, cached  ${if_cache:>10.2f}")
    if if_cache > 0 and paid / if_cache > 1.05:
        print(f"   you paid {paid / if_cache:.0f}x the cached price — this is the $38k bug")
    print(f"   last receipt      {w.prev[:16]}…")
    if args.ledger:
        ok, n, msg = verify_ledger(args.ledger)
        print(f"   ledger {args.ledger}: {'OK' if ok else 'BROKEN'} — {msg}")


def load_records(path):
    text = open(path).read().strip()
    if not text:
        raise SystemExit(f"{path} is empty")
    if text[0] == "[":
        return json.loads(text)
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def cmd_check(args):
    prices = load_prices()
    records = load_records(args.file)
    wallet = Wallet(args.limit) if args.limit else None
    total, trip_at, waste, misses, warnings = 0.0, None, 0.0, 0, set()
    for n, rec in enumerate(records, 1):
        model = rec.get("model") or args.model
        usage = normalize(rec)
        c = cost(model, usage, prices)
        total += c
        warnings.update(sanity(model, usage, prices))
        # the silent killer: a big prompt billed with no cache reads at all
        if usage.get("input", 0) >= 10_000 and usage.get("cache_read", 0) == 0:
            misses += 1
            (_, _, cr, _), _ = unit_cost(model, prices)
            waste += usage["input"] * (prices[model]["in"] / 1e6 - cr)
        if wallet and trip_at is None:
            try:
                wallet.charge(c, request=n, model=model)
            except Tripped:
                trip_at = (n, wallet.spent)  # spent, not total: the blocked request never billed
    print(f"requests   {len(records)}")
    print(f"true cost  ${total:.2f}")
    if misses:
        print(f"cache miss {misses} request(s) billed uncached while sending ≥10k tokens")
        print(f"           ${waste:.2f} of that was avoidable — ~{waste / total * 100:.0f}% of the bill")
    for w in sorted(warnings):
        print(f"warning    {w}")
    if wallet:
        if trip_at:
            kept = total - trip_at[1] + 0
            print(f"at ${wallet.limit:.2f} cap: stopped at request {trip_at[0]}, "
                  f"${kept:.2f} never spent ({(1 - trip_at[1] / total) * 100:.0f}% saved)")
        else:
            print(f"at ${wallet.limit:.2f} cap: would not have tripped")


def cmd_verify(args):
    ok, n, msg = verify_ledger(args.file)
    print(f"{'OK' if ok else 'BROKEN'} — {msg}")
    sys.exit(0 if ok else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="governer", description="hard spend governor for AI agents")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prices", help="what models cost right now")
    p.add_argument("--upcoming", action="store_true", help="announced price changes only")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_prices)

    b = sub.add_parser("box", help="simulated agent in a virtual wallet (no API key)")
    b.add_argument("--limit", type=float, default=10.0)
    b.add_argument("--model", default="gpt-6-astra")
    b.add_argument("--context", type=int, default=200_000)
    b.add_argument("--cache-miss", dest="cache_miss", action="store_true", default=True)
    b.add_argument("--cached", dest="cache_miss", action="store_false")
    b.add_argument("--ledger")
    b.add_argument("--max-turns", type=int, default=5000)
    b.set_defaults(fn=cmd_box)

    c = sub.add_parser("check", help="audit real usage records")
    c.add_argument("file")
    c.add_argument("--model", default="gpt-6-astra")
    c.add_argument("--limit", type=float)
    c.set_defaults(fn=cmd_check)

    v = sub.add_parser("verify", help="check a ledger for tampering")
    v.add_argument("file")
    v.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
