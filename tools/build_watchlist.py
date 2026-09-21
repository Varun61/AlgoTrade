"""
tools/build_watchlist.py

Reads a simple list of symbols from `symbols.txt`, looks up their Angel One tokens,
and automatically updates the `config/settings.yaml` file with the correct format.

Usage:
    python tools/build_watchlist.py
"""

import sys
import yaml
from pathlib import Path

# Add parent directory to path so we can import data.instrument_master
sys.path.insert(0, str(Path(__file__).parent.parent))
from data.instrument_master import download_instrument_master, get_token

SETTINGS_PATH = Path("config/settings.yaml")
SYMBOLS_PATH = Path("symbols.txt")

def update_yaml_watchlist():
    if not SYMBOLS_PATH.exists():
        print(f"Error: {SYMBOLS_PATH} not found. Please create it first.")
        return

    # 1. Read the user's desired symbols
    with open(SYMBOLS_PATH, "r") as f:
        # Ignore empty lines and comments
        raw_symbols = [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]

    print(f"Found {len(raw_symbols)} symbols in symbols.txt to process...")

    # 2. Download the latest Angel One instrument master
    df = download_instrument_master()
    
    new_instruments = []
    missing_symbols = []

    # 3. Look up tokens for each symbol
    for sym in raw_symbols:
        exch = "NSE" 
        
        search_sym = sym
        if not search_sym.endswith("-EQ") and not search_sym.endswith("BEES") and not search_sym.endswith("BEES-EQ"):
             search_sym = f"{search_sym}-EQ"
             
        token = get_token(df, search_sym, exch)
        
        if token:
            new_instruments.append({
                "exchange": exch,
                "segment": 1,
                "token": str(token),
                "symbol": search_sym
            })
        else:
            missing_symbols.append(sym)

    # 4. Update the settings.yaml file
    with open(SETTINGS_PATH, "r") as f:
        config = yaml.safe_load(f)

    if "watchlist" not in config:
        config["watchlist"] = {}
    
    config["watchlist"]["instruments"] = new_instruments

    with open(SETTINGS_PATH, "w") as f:
        yaml.dump(config, f, sort_keys=False, default_flow_style=False)

    print(f"\nSuccessfully updated config/settings.yaml with {len(new_instruments)} instruments!")
    
    if missing_symbols:
        print("\nWarning: Could not find tokens for the following symbols (Check spelling):")
        for m in missing_symbols:
            print(f"   - {m}")
    else:
        print("\nAll symbols matched successfully.")

if __name__ == "__main__":
    update_yaml_watchlist()
