"""
tools/test_bracket_order.py

ONE-TIME LIVE VERIFICATION SCRIPT for OrderManager.place_bracket_order() (Angel
One ROBO/BO orders). This does NOT run as part of main.py or any automated flow.

WHY THIS EXISTS
----------------
Angel One's public SmartAPI docs confirm ROBO/BO order fields exist
(squareoff, stoploss, trailingStopLoss) but do NOT give a worked example, so
two things are unverified and MUST be confirmed with a real tiny order before
the `live-with-bo` branch is trusted with real capital:

  1. Are `squareoff` / `stoploss` interpreted as POINTS away from entry price
     (current assumption in order_manager.py), or as absolute prices?
  2. Does Angel One's BO/ROBO variety actually work for your account/API key,
     and for the specific symbol/exchange you trade (equity intraday)?

WHAT TO DO WITH THIS SCRIPT
----------------------------
  1. Fill in SYMBOL / TOKEN / EXCHANGE below for ONE liquid stock you already
     trade (e.g. from symbols.txt), during market hours.
  2. Set QTY to the smallest tradable lot (1 share for equity).
  3. Run: python -m tools.test_bracket_order
     It will print the computed order params BEFORE sending anything and ask
     for a typed "yes" confirmation — this places a REAL order with REAL money.
  4. After it places the order, go check Angel One's web/app order book:
       - Does the entry leg show at the expected LIMIT price?
       - Do child SL/target legs appear, and at what actual price? Compare
         that price against ENTRY_PRICE +/- STOP_POINTS / +/- TARGET_POINTS
         to confirm whether squareoff/stoploss are points or absolute price.
  5. Manually cancel/exit everything from the Angel One app when done testing
     — do not leave a live bracket order unattended.
  6. Record what you found (units confirmed, whether BO is enabled for your
     account) as a comment at the bottom of this file for future reference.

DO NOT run this unattended, in a loop, or outside market hours.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from auth.session_manager import SessionManager
from execution.order_manager import OrderManager

# ---- Fill these in for a single, tiny, deliberate test -------------------
SYMBOL      = "SBIN-EQ"      # must match the tradingsymbol format Angel One expects
TOKEN       = "3045"         # symboltoken from instrument master
EXCHANGE    = "NSE"
TRANSACTION = "BUY"          # "BUY" or "SELL"
QTY         = 1              # smallest possible lot — this is a REAL order
ENTRY_PRICE = 0.0            # fill with current LTP + small buffer before running
STOP_LOSS   = 0.0            # fill with a stop a few points away from ENTRY_PRICE
TARGET      = 0.0            # fill with a target a few points away from ENTRY_PRICE
# ---------------------------------------------------------------------------


def main() -> None:
    if ENTRY_PRICE <= 0 or STOP_LOSS <= 0 or TARGET <= 0:
        print("ENTRY_PRICE / STOP_LOSS / TARGET must be filled in with real numbers "
              "before running this script. Edit the constants at the top of this file.")
        return

    squareoff_pts = round(abs(TARGET - ENTRY_PRICE), 2)
    stoploss_pts  = round(abs(ENTRY_PRICE - STOP_LOSS), 2)

    print("=== Bracket order (ROBO/BO) live test ===")
    print(f"Symbol       : {SYMBOL} ({EXCHANGE}, token={TOKEN})")
    print(f"Transaction  : {TRANSACTION} x{QTY}")
    print(f"Entry price  : {ENTRY_PRICE}")
    print(f"Stop loss    : {STOP_LOSS}  -> sent as stoploss points = {stoploss_pts}")
    print(f"Target       : {TARGET}  -> sent as squareoff points  = {squareoff_pts}")
    print()
    print("This will place a REAL order with REAL money if you confirm.")
    confirm = input("Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted — no order placed.")
        return

    sm  = SessionManager()
    obj = sm.login()

    order_mgr = OrderManager(smart_obj=obj)
    if order_mgr.mode != "live":
        print(f"\ntrading.mode in config/settings.yaml is '{order_mgr.mode}', not 'live'. "
              "OrderManager would simulate this order instead of sending it. "
              "Set trading.mode: live in settings.yaml if you actually want to test "
              "against the exchange, then re-run.")
        return

    order_id = order_mgr.place_bracket_order(
        symbol=SYMBOL, token=TOKEN, exchange=EXCHANGE,
        transaction=TRANSACTION, qty=QTY,
        entry_price=ENTRY_PRICE, stop_loss=STOP_LOSS, target=TARGET,
        ordertag="bo-test",
    )

    if order_id:
        print(f"\nOrder placed: {order_id}")
        print("Now go check Angel One's order book / app to see the actual "
              "entry + SL + target legs and confirm the units used.")
    else:
        print("\nOrder placement failed — check logs above for the error response.")

    sm.logout()


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# FINDINGS (fill in after running this once):
#   Date tested        :
#   BO enabled for acct :
#   squareoff/stoploss units confirmed as (points / absolute price):
#   Anything else notable:
# ---------------------------------------------------------------------------
