# DESIGN.md — governer docs page

Drawn this run: **Scientific journal** / *designs ferry timetables for the Faroe Islands* / **freight**.
Concept: the page is a printed load manifest. Everything is a table, a figure, or a caption.
A timetable crosses a limit at a row; so does a budget.

## Manifest

- **Colour** — `--paper #f4f5f7` (cool, not cream), `--ink #12151a`, `--quiet #5a5f66`,
  `--rule rgba(18,21,26,.14)`, `--red #8c1c13` (**the single accent, ≤3% of viewport**:
  one rule + one figure number + the tripped amount). No pure black, no pure white.
- **Type** — one family: serif (Georgia / Iowan Old Style / Palatino / Book Antiqua).
  Two ladders that never meet: UI `12 / 13 / 16 / 20 / 24`, display `44 / 60`.
  Weights 400 and 600 only. Display line-height **1.0**, tracking −0.02em.
  Body 16px / 1.55, measure 62ch. `tabular-nums` on every figure column.
  Small-caps labels at 11px with 0.14em tracking.
- **Space** — 4px base. Section padding 96px. Container 1080px, long-form 38rem.
  Internal ≤ external, always.
- **Depth** — none. No shadow anywhere; the 1px hairline is the only depth cue.
  Radius 0px for every element (squareness is the geometry).
- **Motion** — 140ms on link underline only. Nothing scrolls, nothing fades in.
- **Layout** — masthead rule, then a two-column hero split by a vertical hairline:
  claim left, Figure 1 (real terminal output) right. Then the price manifest table.
  ASCII:

```
┌──────────────────────────────────────────────────────────────┐
│ GOVENER                              VOL 1 · 2026-10-26 · MIT│
├───────────────────────────────┬──────────────────────────────┤
│ THE BREAKER                   │ FIG. 1 — RUN, WALLET $10.00  │
│ A budget you cannot           │ turn  1      2.02            │
│ enforce is a suggestion.      │ turn  4      8.10            │
│                               │ turn  5      BLOCKED ─────── │
│ [install line, mono 13px]     │ paid 8.10 · cached 0.90      │
├───────────────────────────────┴──────────────────────────────┤
│ MANIFEST OF PRICES        model | in/M | out/M | cache read  │
│ ──────────────────────────────────────────────────────────── │
│ WHY THE BILL GROWS AT NIGHT (three items, small-caps heads)  │
│ COMMANDS (mono table)                                        │
└──────────────────────────────────────────────────────────────┘
```

## The 20%

1. **Bold move:** the red ledger rule runs full-bleed at the exact row where the breaker
   tripped, with the amount printed on the line — a balance line in a ledger book.
2. **Voice:** "A budget you cannot enforce is a suggestion." Manifest language, no
   exclamation, no "supercharge".
3. **Micro-detail:** the real receipt hash prefix from a live run, printed in mono under
   the figure — only someone who has actually run the proxy has one.
4. **Product-specific:** the price column marks convention-derived cache prices with `~`,
   which is the actual behaviour of `governer prices`.

## Collision probe

Same manifest for a different brief (a photo-sharing app) would give radius, shadows and
an accent-heavy hero. This one gives 0px, no shadow, one red line. No collision.
