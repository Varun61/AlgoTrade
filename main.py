"""
main.py — options trading entrypoint (optionsbranch).

This branch is OPTIONS-ONLY. The old equity intraday orchestrator has been
removed. What lives here now:

  Research / sizing (works today, real NSE data):
    python -m options.runner                 # size the strategy in settings.yaml
    python -m options.runner --compare       # compare weekly / daily / hybrid
    python -m backtest.options_multi_bt      # weekly structures
    python -m backtest.options_intraday_bt   # 0DTE same-day structures

  Paper trading (no real orders):
    python -m tools.run_options_paper

  Data:
    python -m tools.fetch_nse_fo --start 2021-07-16 --end <today>

LIVE trading is intentionally NOT wired yet: multi-leg options order placement
has not been written, and `options.live_trading_authorized` in settings.yaml is
the hard gate. Until that exists and forward paper-trading confirms real fills,
running live is blocked by design.
"""
from __future__ import annotations

import sys


def main() -> None:
    import yaml
    from pathlib import Path

    cfg = yaml.safe_load(open(Path(__file__).parent / "config" / "settings.yaml"))
    opt = (cfg or {}).get("options", {})
    if (cfg or {}).get("trading", {}).get("mode") == "live" or opt.get("live_trading_authorized"):
        print("LIVE options trading is not implemented on this branch. "
              "Order-placement code does not exist yet and the strategy is not "
              "live-verified. Use `python -m options.runner` for sizing and "
              "`python -m tools.run_options_paper` for paper trading.")
        sys.exit(1)
    print(__doc__)


if __name__ == "__main__":
    main()
