"""
options/ — the config-driven NIFTY options trading system.

Everything the user runs for real money lives here:
  registry.py  — the single source of truth for every tradable strategy
                 (daily / weekly / hybrid, extensible), each with its MINIMUM
                 CAPITAL and per-lot margin documented.
  sizer.py     — turns a strategy + your capital into a concrete lot plan
                 (how many lots, margin used, capital deployed) at a chosen
                 risk budget.
  runner.py    — reads config/settings.yaml `options:` block, picks the strategy
                 by NAME, sizes it, and reports expected profit / drawdown / margin.

Add a new strategy in ONE place (registry.py) and it becomes selectable by name
in settings.yaml — no other code changes needed.
"""
