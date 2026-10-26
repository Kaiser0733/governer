#!/usr/bin/env python3
"""governer — hard spend governor for AI agents.

Budget alerts are not kill switches. governer prices every request the way
the provider actually bills it (cache reads and writes included), then stops
the agent at the line you set.

Zero dependencies. One file. Python 3.9+.

  governer prices [--upcoming] [--json]
  governer free
  governer box    [--limit 10] [--model gpt-6-astra] [--context 200000]
  governer proxy  [--limit 5] [--port 8787] [--upstream URL]
  governer check  usage.json [--limit 200] [--fail-over 50]
  governer verify ledger.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES_PATH = os.environ.get("GOVERNER_PRICES", os.path.join(HERE, "prices.json"))
LIVE_PATH = os.environ.get("GOVERNER_LIVE", os.path.join(HERE, "prices.live.json"))

__version__ = "1.0.0"

# Provider logs spell the same field three ways. Accept all of them.
ALIASES = {
    "input": ("input", "input_tokens", "prompt_tokens"),
    "output": ("output", "output_tokens", "completion_tokens"),
    "cache_read": ("cache_read", "cache_read_input_tokens", "cached_tokens"),
    "cache_write": ("cache_write", "cache_creation_input_tokens"),
}


# Installed copies (pipx, site-packages) have no prices.json next to the module,
# so the tool still works out of the box. Keep in sync with prices.json —
# test_embedded_prices_match_the_file fails the moment these drift.
_FALLBACK = {
    "claude-fable-5-1": {"in": 10.0, "out": 50.0},
    "claude-opus-5-5": {"in": 4.0, "out": 20.0},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50},
    "gpt-6-astra": {"in": 10.0, "out": 50.0},
    "gpt-6-1-sol": {"in": 2.0, "out": 10.0},
    "gpt-6-luna": {"in": 0.10, "out": 0.50},
    "gemini-3-8-flash": {"in": 0.75, "out": 3.75},
    "gemini-4-argon": {"in": 2.0, "out": 10.0},
    "grok-4-7": {"in": 2.0, "out": 6.0, "tiered_above": 200000},
    "deepseek-v4-pro": {"in": 1.32, "out": 3.96, "peak_pricing": True},
    "qwen3-5-plus": {"in": 0.40, "out": 2.40, "tiered_above": 256000},
    "llama-5-hosted": {"in": 0.35, "out": 0.35},
}


def load_curated(path=PRICES_PATH):
    try:
        with open(path) as f:
            return json.load(f)["models"]
    except OSError:
        return dict(_FALLBACK)


def load_prices(path=PRICES_PATH):
    """Curated table first (hand-verified), then anything a live refresh found."""
    models = dict(load_curated(path))
    if os.path.exists(LIVE_PATH):
        with open(LIVE_PATH) as f:
            for name, row in json.load(f)["models"].items():
                models.setdefault(name, row)
    return models


def fetch_live(api):
    """Everything the gateway knows, with per-token prices turned into per-million."""
    with urllib.request.urlopen(api.rstrip("/") + "/models", timeout=60) as r:
        data = json.load(r)["data"]
    models = {}
    for m in data:
        pr = m.get("pricing") or {}

        def per_m(key):
            try:
                return round(float(pr[key]) * 1e6, 6)
            except (KeyError, TypeError, ValueError):
                return None

        row = {"in": per_m("prompt"), "out": per_m("completion"),
               "cache_read": per_m("input_cache_read"),
               "cache_write": per_m("input_cache_write"),
               "ctx_in": m.get("context_length"), "name": m.get("name"),
               "expires": m.get("expiration_date"),
               "verified": now()[:10], "source": "openrouter.ai/api/v1/models"}
        # routers report -1 when they cannot quote a price; skip them, they are not models
        if row["in"] is not None and row["out"] is not None \
                and row["in"] >= 0 and row["out"] >= 0:
            models[m["id"]] = row
    return models


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
    def __init__(self, spent, limit, would_be, reason=""):
        self.spent, self.limit, self.would_be = spent, limit, would_be
        self.reason = reason
        super().__init__(
            f"tripped at ${spent:.4f} of ${limit:.2f}; "
            f"request blocked (would have cost ${would_be - spent:.4f})"
            + (f" — {reason}" if reason else ""))


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
        return self.record(amount, **meta)

    def record(self, amount, **meta):
        """Book spend that already happened, even past the limit. Reporting only."""
        self.spent += amount
        rec = {"ts": now(), "cost": round(amount, 8), "total": round(self.spent, 8),
               "prev": self.prev, **meta}
        rec["hash"] = _hash(rec)
        self.prev = rec["hash"]
        if self.path:
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
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
    return True, n, f"{n} records · ${total:.4f} · chain intact"


class Treasury:
    """One total budget plus per-agent caps, all checked before a request leaves.

    Agents are identified by the `X-Governer-Agent` header. A cap with
    `"per": "day"` resets at UTC midnight; `"per": "run"` lasts for the process
    and survives a restart by re-reading today's ledger records.
    """

    def __init__(self, limit, path=None, agents=None):
        self.total = Wallet(limit, path)
        self.agents = {name: {"limit": float(cfg.get("limit", 0)),
                              "spent": 0.0,
                              "per": cfg.get("per", "run")}
                       for name, cfg in (agents or {}).items()}
        self.today = now()[:10]
        self._rehydrate()

    @property
    def spent(self):
        return self.total.spent

    @property
    def limit(self):
        return self.total.limit

    def _rehydrate(self):
        """Rebuild per-agent counters from the ledger so restarts cannot reset a cap."""
        if not self.total.path or not os.path.exists(self.total.path):
            return
        try:
            records = read_ledger(self.total.path)
        except (OSError, json.JSONDecodeError):
            return  # an unreadable ledger must not stop the proxy from starting
        for rec in records:
            a = self.agents.get(rec.get("agent"))
            if not a:
                continue
            if a["per"] == "run" or str(rec.get("ts", ""))[:10] == self.today:
                a["spent"] += rec.get("cost", 0)

    def _roll_day(self):
        today = now()[:10]
        if today != self.today:
            self.today = today
            for a in self.agents.values():
                if a["per"] == "day":
                    a["spent"] = 0.0

    def blocker(self, agent, amount):
        """Why this charge would be refused, or None. Read-only."""
        if self.total.spent + amount > self.total.limit:
            return f"total budget {money(self.total.spent)} of {money(self.total.limit)}"
        a = self.agents.get(agent)
        if a and a["limit"] > 0 and a["spent"] + amount > a["limit"]:
            return f"agent '{agent}' {money(a['spent'])} of {money(a['limit'])} per {a['per']}"
        return None

    def charge(self, agent, amount, **meta):
        self._roll_day()
        why = self.blocker(agent, amount)
        if why:
            raise Tripped(self.total.spent, self.total.limit,
                          self.total.spent + amount, why)
        return self._book(agent, amount, meta)

    def force_charge(self, agent, amount, **meta):
        """Book spend that already left the account, or a dry-run overrun."""
        self._roll_day()
        return self._book(agent, amount, meta)

    def _book(self, agent, amount, meta):
        rec = self.total.record(amount, agent=agent, **meta)
        if agent in self.agents:
            self.agents[agent]["spent"] += amount
        return rec

    def status(self):
        return {"version": __version__,
                "spent": round(self.spent, 6), "limit": self.limit,
                "remaining": round(self.limit - self.spent, 6),
                "tripped": self.spent >= self.limit,
                "agents": {name: {"spent": round(a["spent"], 6),
                                  "limit": a["limit"], "per": a["per"]}
                           for name, a in self.agents.items()}}


def notify(url, payload, timeout=5):
    """Fire-and-forget webhook. A broken webhook must never break the breaker."""
    def send():
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
            req.add_header("content-type", "application/json")
            urllib.request.urlopen(req, timeout=timeout).read()
        except Exception:
            pass

    threading.Thread(target=send, daemon=True).start()


def load_config(path):
    if not path:
        return {}
    with open(path) as f:
        conf = json.load(f)
    if not isinstance(conf, dict):
        raise SystemExit(f"{path}: config must be a JSON object")
    return conf


# ---------------------------------------------------------------- commands

def cmd_prices(args):
    if args.refresh:
        models = fetch_live(args.api)
        with open(LIVE_PATH, "w") as f:
            json.dump({"refreshed": now(), "source": args.api, "models": models},
                      f, indent=1, sort_keys=True)
        free = sum(1 for m in models.values() if m["in"] == 0 and m["out"] == 0)
        cached = sum(1 for m in models.values() if m["cache_read"])
        print(f"{len(models)} models refreshed from {args.api}")
        print(f"  free right now: {free}")
        print(f"  vendor-confirmed cache pricing: {cached}")
        print(f"  saved to {LIVE_PATH}")
        return
    prices = load_curated()
    if args.live:
        prices = load_prices()
        rows = sorted((p for p in prices.values() if p.get("in") is not None),
                      key=lambda p: (p["in"], p["out"]))
        if not args.all:
            rows = rows[:40]
        print(f"{'model':<50} {'in/M':>9} {'out/M':>9} {'cache r':>9}       ctx")
        for p in rows:
            cr = f"{p['cache_read']:.4f}" if p.get("cache_read") is not None else "-"
            print(f"{(p.get('name') or '?')[:50]:<50} {p['in']:>9.4f} {p['out']:>9.4f} "
                  f"{cr:>9}  {p.get('ctx_in') or '?':>9}")
        print(f"\n{len(rows)} of {len(prices)} models — "
              f"`governer prices --refresh` pulls what the gateway knows today")
        return
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
        if total > 0:
            print(f"           ${waste:.2f} of that was avoidable — ~{waste / total * 100:.0f}% of the bill")
    for w in sorted(warnings):
        print(f"warning    {w}")
    if wallet:
        if trip_at:
            kept = total - trip_at[1]
            saved = f" ({(1 - trip_at[1] / total) * 100:.0f}% saved)" if total > 0 else ""
            print(f"at ${wallet.limit:.2f} cap: stopped at request {trip_at[0]}, "
                  f"${kept:.2f} never spent{saved}")
        else:
            print(f"at ${wallet.limit:.2f} cap: would not have tripped")
    if args.fail_over is not None and total > args.fail_over:
        print(f"FAIL — ${total:.2f} is over the ${args.fail_over:.2f} gate")
        sys.exit(1)


def cmd_verify(args):
    ok, n, msg = verify_ledger(args.file)
    print(f"{'OK' if ok else 'BROKEN'} — {msg}")
    sys.exit(0 if ok else 1)


def cmd_free(args):
    """Live check: which models are free right now (OpenRouter's public catalogue)."""
    with urllib.request.urlopen(args.api.rstrip("/") + "/models", timeout=30) as r:
        data = json.load(r)["data"]
    free = [m for m in data
            if str(m.get("pricing", {}).get("prompt")) in ("0", "0.0", "0.00")
            and str(m.get("pricing", {}).get("completion")) in ("0", "0.0", "0.00")]
    print(f"{len(free)} of {len(data)} models are free right now ({now()[:10]})")
    for m in sorted(free, key=lambda m: -(m.get("context_length") or 0)):
        print(f"  {m['id']:<52} ctx {m.get('context_length') or '?':>9}")
    print("\nfree tiers expire without notice — re-run this before trusting one")


# ---------------------------------------------------------------- the proxy

_lock = threading.Lock()


def money(x):
    return f"${x:.4f}" if 0 < x < 0.01 else f"${x:.2f}"


def estimate_tokens(body):
    """~4 chars per token. ponytail: no tokenizer, so pre-flight is an estimate."""
    text = body.get("prompt") or ""
    if isinstance(text, list):
        text = " ".join(str(t) for t in text)
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            text += c
        elif isinstance(c, list):
            text += "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return max(1, len(text) // 4) if text else 0


def make_handler(tr, cfg):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _out(self, code, payload, extra=()):
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, code, obj):
            self._out(code, json.dumps(obj).encode())

        def do_GET(self):
            if self.path.startswith("/__governer"):
                self._json(200, tr.status())
            else:
                self._json(404, {"error": {"message": "governer proxies POST only"}})

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            if length > 64 * 1024 * 1024:
                return self._json(413, {"error": {"message": "governer: body over 64MB"}})
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json(400, {"error": {"message": "governer: body is not JSON"}})
            if not isinstance(body, dict):
                return self._json(400, {"error": {"message":
                                                  "governer: body must be a JSON object"}})
            model = body.get("model") or ""
            agent = (self.headers.get("x-governer-agent") or "default")[:64]
            if body.get("stream"):
                return self._json(400, {"error": {"message":
                    "governer: stream is not supported yet — send stream:false "
                    "(the wallet needs the final usage block to bill accurately)"}})

            # 1. worst-case price BEFORE the request leaves the machine
            est_in = estimate_tokens(body)
            est_out = int(body.get("max_tokens") or body.get("max_completion_tokens") or 1024)
            i_rate, o_rate = cfg["rate"](model)
            estimate = est_in * i_rate + est_out * o_rate
            with _lock:
                why = tr.blocker(agent, estimate)
            if why and cfg["dry_run"]:
                print(f"  ◌ would refuse {agent}/{model or '(unnamed)'} — {why}")
            elif why:
                print(f"  🛑 refused {agent}/{model or '(unnamed)'} — {why}")
                cfg["alert"]({"event": "breaker_tripped", "agent": agent, "model": model,
                              "reason": why, "estimate": round(estimate, 6), **tr.status()})
                return self._json(402, {"error": {
                    "type": "governer_breaker_tripped",
                    "message": f"governer refused this request: {why}; "
                               f"worst case here is ${estimate:.4f}",
                    "agent": agent, "spent": round(tr.spent, 6), "limit": tr.limit}})

            # 2. forward, passing the caller's auth through
            path = self.path
            up = cfg["upstream"].rstrip("/")
            if up.endswith("/v1") and path.startswith("/v1"):
                path = path[3:]  # upstream already carries /v1
            req = urllib.request.Request(up + path, data=raw, method="POST")
            for h in ("authorization", "content-type", "x-api-key", "anthropic-version",
                      "http-referer", "x-title"):
                v = self.headers.get(h)
                if v:
                    req.add_header(h, v)
            if not self.headers.get("authorization") and cfg.get("key"):
                req.add_header("authorization", "Bearer " + cfg["key"])
            try:
                with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
                    payload, code = r.read(), r.status
            except urllib.error.HTTPError as e:
                payload, code = e.read(), e.code
            except Exception as e:
                return self._json(502, {"error": {"message": f"governer: upstream failed: {e}"}})

            # 3. bill what actually happened
            try:
                raw_usage = json.loads(payload).get("usage") or {}
                usage = normalize(raw_usage)
                if usage:
                    actual = (usage.get("input", 0) * i_rate + usage.get("output", 0) * o_rate
                              + usage.get("cache_read", 0) * i_rate * 0.1
                              + usage.get("cache_write", 0) * i_rate * 1.25)
                    # the vendor's own number, when it reports one, is an independent check
                    vendor = raw_usage.get("cost")
                    if isinstance(vendor, (int, float)) and vendor > 0 \
                            and abs(vendor - actual) > max(1e-6, actual * 0.02):
                        print(f"  ⚠ price mismatch: governer ${actual:.6f} vs vendor ${vendor:.6f} "
                              f"— your prices.json is stale")
                    with _lock:
                        why = tr.blocker(agent, actual)
                        if why and cfg["dry_run"]:
                            print(f"  ◌ would overrun — {why}")
                            tr.force_charge(agent, actual, model=model, usage=usage,
                                            status=code, vendor_cost=vendor, dry_run=True)
                        elif why:  # the estimate was low and the money is already gone
                            print(f"  ⚠ estimate was low — {why}")
                            tr.force_charge(agent, actual, model=model, usage=usage,
                                            status=code, vendor_cost=vendor)
                            cfg["alert"]({"event": "overrun", "agent": agent, "model": model,
                                          "reason": why, **tr.status()})
                        else:
                            tr.charge(agent, actual, model=model, usage=usage,
                                      status=code, vendor_cost=vendor)
                    print(f"  ✓ {code} {agent}/{model[:34]:<34} ${actual:.6f}  "
                          f"${tr.spent:.4f}/${tr.limit:.2f}")
            except Exception as e:
                print(f"  ⚠ could not price response: {e}")

            self._out(code, payload, [("x-governer-spent", f"{tr.spent:.6f}"),
                                      ("x-governer-limit", f"{tr.limit:.2f}"),
                                      ("x-governer-agent", agent)])

        def log_message(self, *a):
            pass

    return Handler


def cmd_proxy(args):
    conf = load_config(args.config)
    limit = args.limit if args.limit is not None else conf.get("limit", 5.0)
    upstream = args.upstream or conf.get("upstream") or "https://openrouter.ai/api/v1"
    ledger = args.ledger or conf.get("ledger")
    d_in = args.default_in if args.default_in is not None else conf.get("default_in", 0.0)
    d_out = args.default_out if args.default_out is not None else conf.get("default_out", 0.0)
    dry = bool(args.dry_run or conf.get("dry_run"))
    webhook = args.notify or conf.get("notify")
    prices = load_prices()
    tr = Treasury(limit, ledger, conf.get("agents") or {})

    def rate(model):
        if model in prices:
            (i, o, _, _), _ = unit_cost(model, prices)
            return i, o
        return d_in / 1e6, d_out / 1e6  # ponytail: unknown model = flat rate
    def alert(payload):
        if webhook:
            notify(webhook, payload)

    cfg = {"upstream": upstream, "rate": rate, "timeout": args.timeout,
           "dry_run": dry, "alert": alert,
           "key": args.key or os.environ.get("GOVENER_KEY")}
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(tr, cfg))
    base = f"http://{args.host}:{args.port}"
    print(f"governer {__version__}  {base}  →  {upstream}")
    print(f"budget {money(tr.limit)}"
          f"{'  (dry run: counts, never refuses)' if dry else ''}"
          f" · unknown models ${d_in}/${d_out} per M")
    for name, a in tr.agents.items():
        print(f"  agent {name:<22} {money(a['limit'])} per {a['per']}")
    if ledger:
        print(f"ledger {ledger}")
    if webhook:
        print(f"alerts {webhook}")
    print(f"\n  export OPENAI_BASE_URL={base}/v1")
    print( "  export OPENAI_API_KEY=<your key>     # governer never stores it")
    print(f"  curl {base}/__governer\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        msg = verify_ledger(ledger)[2] if ledger else "no ledger"
        print(f"\nstopped · spent {money(tr.spent)} of {money(tr.limit)} · {msg}")


def main(argv=None):
    try:  # a proxied log file must show lines as they happen, not at exit
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(prog="governer", description="hard spend governor for AI agents")
    ap.add_argument("--version", action="version", version=f"governer {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("prices", help="what models cost right now")
    p.add_argument("--upcoming", action="store_true", help="announced price changes only")
    p.add_argument("--refresh", action="store_true", help="pull live prices from a gateway")
    p.add_argument("--live", action="store_true", help="list every known model, cheapest first")
    p.add_argument("--all", action="store_true", help="do not truncate --live")
    p.add_argument("--api", default="https://openrouter.ai/api/v1")
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
    c.add_argument("--fail-over", dest="fail_over", type=float,
                   help="exit 1 if true cost exceeds this (CI gate)")
    c.set_defaults(fn=cmd_check)

    v = sub.add_parser("verify", help="check a ledger for tampering")
    v.add_argument("file")
    v.set_defaults(fn=cmd_verify)

    f = sub.add_parser("free", help="which models are free right now (live)")
    f.add_argument("--api", default="https://openrouter.ai/api/v1")
    f.set_defaults(fn=cmd_free)

    x = sub.add_parser("proxy", help="OpenAI-compatible shim that enforces the wallet")
    x.add_argument("--config", help="JSON config: limit, upstream, ledger, agents, notify, dry_run")
    x.add_argument("--limit", type=float)
    x.add_argument("--port", type=int, default=8787)
    x.add_argument("--host", default="127.0.0.1")
    x.add_argument("--upstream")
    x.add_argument("--ledger")
    x.add_argument("--notify", help="webhook URL to POST to when the breaker trips")
    x.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="count everything, refuse nothing (watch before you enforce)")
    x.add_argument("--key", help="upstream key, if the client does not send one")
    x.add_argument("--default-in", dest="default_in", type=float,
                   help="USD per M for models missing from the price tables (0 = assume free)")
    x.add_argument("--default-out", dest="default_out", type=float)
    x.add_argument("--timeout", type=float, default=120)
    x.set_defaults(fn=cmd_proxy)

    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        ap.print_help()
        sys.exit(2)
    args.fn(args)


if __name__ == "__main__":
    main()
