# Changelog

## 1.0.2 — 2026-10-26

The "would this make someone poor?" audit. Two of these could have.

- **Parallel requests could overspend a budget.** Eight simultaneous calls each saw the
  same remaining balance, all passed the check, all billed — several times over the cap.
  Estimates are now *held* while a request is in flight and settled against real usage.
- **Streaming is supported.** It used to be refused, which made governer unusable with
  the agent clients that stream by default. SSE is passed through untouched, with
  `stream_options.include_usage` injected so the stream can still be billed.
- **A request with no `usage` block was booked as free.** It now books the estimate
  instead — for a money guard, assume the money is gone.
- **A timeout was booked as free.** Same fix: if the provider may have run it, the
  estimate is charged and flagged `assumed`.
- **An unpriced model was enforced at $0.** It now warns on first sight, with the rate
  it is using.
- **New: `governer status`** — the meter. Spent, burn rate, projection, per-agent split,
  and chain check. It refuses to project from a window under 30 minutes instead of
  inventing a number.

## 1.0.1 — 2026-10-26

Billing correctness. Found by auditing the money path line by line, not by a report.

- **OpenAI and OpenRouter were double-billed.** `prompt_tokens` already contains
  the cached tokens, so charging `input` *and* `cache_read` billed the same tokens
  twice — up to 10× over on a cache-heavy call. Records from `prompt_tokens` are
  now treated as inclusive; Anthropic's `input_tokens` stays exclusive.
- **Nested cache counters are read.** `prompt_tokens_details.cached_tokens` was
  ignored entirely, so cached traffic looked like full-price input.
- **The proxy used hardcoded cache rates (0.1× / 1.25×)** instead of the model's
  real cache price from the table. It now uses the table.
- **The pre-flight estimate copied the whole request into one string**, which on a
  64MB body is gigabytes of churn. It now counts characters.
- **An unpriced model in a log killed the whole audit.** It is now skipped and
  reported as a count.
- **Logs are streamed**, so auditing a 2GB file does not need 2GB of RAM.
- **Malformed `content-length` and `max_tokens` return 400** instead of a traceback.
- An unreadable ledger now warns on stderr instead of silently starting every cap at zero.
- The unknown-model error lists 12 examples, not all 472.

## 1.0.0 — 2026-10-26

First release that can be trusted in front of an unattended agent.

- **Breaker that refuses before spending.** The proxy prices the worst case
  before a request leaves the machine, bills the vendor's real `usage` block
  after, and returns `402 governer_breaker_tripped` when the wallet would be
  crossed. Refused requests never reach the provider.
- **Per-agent and per-day budgets** from a JSON config (`governer.example.json`),
  keyed by the `X-Governer-Agent` header. Caps survive a restart by re-reading
  today's ledger.
- **Dry-run mode** (`--dry-run`): books the true spend, refuses nothing, so you
  can watch a week of real traffic before enforcing.
- **Webhook alerts** on trip and on estimate overrun; a broken webhook can never
  break the breaker.
- **Live price oracle**: `prices --refresh` pulls ~459 models from the gateway,
  313 of them with vendor-confirmed cache pricing; unknown models still get
  priced instead of silently costing nothing.
- **Audit** (`check`) finds cache misses and the avoidable dollars behind them.
- **Tamper-evident ledger**: append-only, hash-chained, fsynced, `verify` detects
  any edited receipt.
- **CI gate**: `check --fail-over` plus a composite GitHub Action.
- **Agent Skill** (`SKILL.md`) so coding agents price and gate their own runs.
- Hardening: 64MB body cap, malformed bodies get `400` not a traceback,
  streaming refused with a clear reason, concurrency-safe charging.

## 0.2.0

- OpenAI-compatible proxy, live `free` catalogue, `check --fail-over`, GitHub Action.

## 0.1.0

- Cache-aware cost math, hard wallet, hash-chained ledger, `box` demo, `prices`.
