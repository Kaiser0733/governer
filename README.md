# governer

[![test](https://github.com/Kaiser0733/governer/actions/workflows/test.yml/badge.svg)](https://github.com/Kaiser0733/governer/actions/workflows/test.yml)

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

No API key, no network, ninety seconds.

## Install

```bash
curl -O https://raw.githubusercontent.com/Kaiser0733/governer/main/governer.py
curl -O https://raw.githubusercontent.com/Kaiser0733/governer/main/prices.json
python3 governer.py box
```

No pip, no dependencies, one file plus one price table. Python 3.9+.

## Commands

| command | what it does |
|---|---|
| `governer proxy --limit 5` | OpenAI-compatible shim: point your agent at it, it refuses anything past $5 |
| `governer box` | simulated agent in a virtual wallet — watch the breaker trip, no key needed |
| `governer check usage.json --limit 20` | true cost of a real log, plus where a $20 cap would have stopped it |
| `governer prices` | what every model costs right now, with its verified date |
| `governer prices --upcoming` | announced price changes — Gemini 3.8 Flash doubles 2027-01-01 |
| `governer free` | which models are free *right now*, fetched live |
| `governer verify ledger.jsonl` | checks a spend ledger for tampering |

## Put it in front of your agent

```bash
$ governer proxy --limit 5 --ledger spend.jsonl

governer http://127.0.0.1:8787  →  https://openrouter.ai/api/v1
wallet $5.00 · unknown models billed $0.0/$0.0 per M

  export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
  export OPENAI_API_KEY=<your key>     # governer never stores it
```

Your agent keeps working unchanged. Every request is priced at its worst case
*before it leaves the machine*, forwarded with your own key, then billed at its
real usage. Cross the limit and the request comes back `402` with
`type: governer_breaker_tripped` — and your provider never sees it.

Streaming is refused for now: the wallet needs the final `usage` block to bill
accurately. Unpriced models fall back to `--default-in/--default-out`, which is
also how you *shadow-price* a free model to see what it would have cost:

```
  ✓ 200 nvidia/nemotron-3.5-lightning:free    $0.001230  $0.0012/$5.00
```

When the provider reports its own `cost` field, governer compares it against
its own number and warns when `prices.json` has gone stale.

## Audit a log you already have

```
$ governer check usage.json --limit 20
requests   100
true cost  $52.00
cache miss 100 request(s) billed uncached while sending ≥10k tokens
           $43.20 of that was avoidable — ~83% of the bill
at $20.00 cap: stopped at request 39, $32.24 never spent (62% saved)
```

Reads Anthropic (`input_tokens`), OpenAI (`prompt_tokens`) and OpenRouter
(`cached_tokens`) field names, as a JSON array or JSONL.

## Gate a pull request on cost

```yaml
- uses: Kaiser0733/governer@main
  with:
    usage-file: ci/agent-usage.json
    budget: "2.50"
```

`governer check --fail-over 2.50` exits `1` when the run costs more. Tests
should not be the only thing a PR can fail.

## Why the bill explodes at night

- **1M-token contexts are table stakes**, so the cached prefix is replayed on
  every call. Cache reads dominate agent bills — and they are billed even when
  that content is truncated before the model attends to it.
- **"Prompt caching is supported" is not "your stack is caching."** The
  expensive failure is silent: status 200, normal-looking output, invoice 100×.
- **Alerts are not limits.** Email after the money is gone is not a safety
  mechanism, and neither are credits or a dashboard you check on Mondays.

## Receipts

The spend ledger is append-only and hash-chained: every record carries the hash
of the one before it, so editing a number breaks the chain and `verify` says so.
Tamper-evident bookkeeping, because a budget you can quietly rewrite is not a
budget.

## Prices are data, not truth

Every model in `prices.json` carries a `verified` date and a source. Cache
prices marked `~` are derived from the vendor convention (0.1× read, 1.25×
write) rather than confirmed. Re-verify before betting a number that matters,
and open a PR when a vendor moves.

## Roadmap

- ~~v0.2~~ proxy, live `free` check, CI gate — shipped
- **v0.3** per-agent and per-workflow budgets on one proxy
- **v0.4** phone push the second a breaker trips
- **v0.5** price table refreshed from vendor APIs instead of hand-maintained

## License

MIT. Built by [Kaiser0733](https://github.com/Kaiser0733) at Vastraa Labs.
