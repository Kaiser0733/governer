# Changelog

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
