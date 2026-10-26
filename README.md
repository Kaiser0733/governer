# governer

**Your agent spent $37,901 while you slept. The budget alert told you afterwards.**

`governer` prices every request the way the provider actually bills it — cache
reads and writes included — then **refuses the request that would cross your
line**. Not a warning. Not a dashboard. A kill switch.

```
$ governer box
wallet $10.00 · gpt-6-astra · 200,000 tok/turn · CACHE MISSING

  turn    1   $    2.02 / $10.00
  turn    2   $    4.05 / $10.00
  turn    3   $    6.07 / $10.00
  turn    4   $    8.10 / $10.00
  turn    5   BLOCKED  (would have cost $2.0250)

🛑 BREAKER TRIPPED at $8.10 of $10.00 — agent stopped

   paid              $      8.10  (5 turns)
   same run, cached  $      0.90
   you paid 9x the cached price — this is the $38k bug
```

No API key. No network. Ninety seconds.

## Install

```bash
curl -O https://raw.githubusercontent.com/Kaiser0733/governer/main/governer.py
curl -O https://raw.githubusercontent.com/Kaiser0733/governer/main/prices.json
python3 governer.py box
```

No pip, no dependencies, one file plus one price table.

## Commands

| command | what it does |
|---|---|
| `governer box` | simulated agent in a virtual wallet — watch the breaker trip, no key needed |
| `governer check usage.json` | true cost of a real log; finds requests billed uncached and how many dollars that wasted |
| `governer check usage.json --limit 20` | shows where a $20 cap would have stopped it and what you'd have saved |
| `governer prices` | what every model costs right now, with the verified date |
| `governer prices --upcoming` | announced price changes — Gemini 3.8 Flash doubles on 2027-01-01 |
| `governer verify ledger.jsonl` | checks a spend ledger for tampering |

`check` accepts Anthropic (`input_tokens`), OpenAI (`prompt_tokens`) and
`cached_tokens` field names, as JSON array or JSONL.

```
$ governer check usage.json --limit 20
requests   100
true cost  $52.00
cache miss 100 request(s) billed uncached while sending ≥10k tokens
           $43.20 of that was avoidable — ~83% of the bill
at $20.00 cap: stopped at request 39, $31.72 never spent (61% saved)
```

## Why the bill explodes at night

- **1M-token contexts are table stakes**, so the whole cached prefix is replayed
  on every call. Cache reads dominate agent bills — and they are billed even
  when that content is truncated before the model attends to it.
- **"Prompt caching is supported" is not "your stack is caching."** The
  expensive failure is silent: status 200, normal-looking output, invoice 100×.
- **Alerts are not limits.** Email after the money is gone is not a safety
  mechanism, and neither are credits or a dashboard you check on Mondays.

## What v0.1 does

- cache-aware true cost: input, output, cache read, cache write
- a hard wallet that raises **before** the money moves
- hash-chained append-only ledger, tamper-evident, `verify` checks the chain
- log audit: cache-miss detection and the avoidable dollars it cost
- price table with per-model verified date and source
- upcoming price-change warnings, so next quarter's invoice is not a surprise

## Roadmap

- **v0.2** OpenAI-compatible proxy — same wallet in front of your agent, no code change
- **v0.3** GitHub Action — fail a PR that raises cost instead of tests
- **v0.4** phone push the second a breaker trips

## Prices are data, not truth

Every model in `prices.json` carries a `verified` date and a source. Cache
prices marked `~` are derived from the vendor convention (0.1× read, 1.25×
write) rather than confirmed — re-verify before betting a number that matters,
and open a PR when a vendor moves.

## License

MIT. Built by [Kaiser0733](https://github.com/Kaiser0733) at Vastraa Labs.
