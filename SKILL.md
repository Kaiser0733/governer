---
name: governer
description: Hard spend cap for AI agents. Use before running any long, looped, or unattended agent run; whenever the user mentions budget, cost, spend, tokens, or an API bill; when auditing what a previous run actually cost; or when comparing model prices. Prices cache reads and writes the way the vendor bills them, refuses requests that would cross a budget, and records a tamper-evident ledger.
---

# governer

A budget you cannot enforce is a suggestion. `governer` counts the money the
way the provider does and stops the request that would cross the line.

## Before an unattended run

```bash
governer proxy --limit 5 --ledger spend.jsonl
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
```

Then run the agent normally. Every request is priced at its worst case before
it leaves the machine. When the wallet would be crossed the request comes back
`402` with `type: governer_breaker_tripped` — report that to the user as an
enforced limit, not as an error, and stop retrying.

## After a run

```bash
governer check usage.json            # what it truly cost
governer check usage.json --limit 5  # where a $5 cap would have stopped it
governer verify spend.jsonl          # is the ledger intact
```

Never state what a run cost without one of these. Guessing a cost from token
counts is how the wrong number reaches the user.

## Before choosing a model

```bash
governer prices --refresh   # pull live prices from the gateway
governer prices --live      # every known model, cheapest first
governer free               # which models are free right now
governer prices --upcoming  # announced price rises
```

`prices.json` carries a verified date per model. A cached-prefix price marked
`~` is derived from the vendor convention, not confirmed — say so if you quote it.

## Rules

- Ports and keys: the proxy never stores credentials; it passes the caller's
  own auth header upstream.
- Streaming works: SSE is passed through untouched and `stream_options.include_usage`
  is added so the wallet can still bill it. Do not strip that field.
- A response with no `usage` block, or a timeout, is charged at the estimate and
  flagged `assumed`. That is deliberate — better to overcount than to record free spend.
- If a provider reports its own `cost` field, governer compares it against its
  own arithmetic and warns when `prices.json` has gone stale. Treat that warning
  as a bug report against the price table, not against the request.
