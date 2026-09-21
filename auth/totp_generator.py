"""
auth/totp_generator.py

Standalone TOTP code generator — useful for debugging auth issues.
Also provides a helper to verify your TOTP secret is correct.

Usage:
    python -m auth.totp_generator
"""

import os, sys
import pyotp
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / "secrets.env")


def get_current_totp(secret: str | None = None) -> str:
    """Generate the current 6-digit TOTP code."""
    secret = secret or os.environ["ANGEL_TOTP_SECRET"]
    return pyotp.TOTP(secret).now()


def verify_totp_secret(secret: str) -> bool:
    """
    Quick sanity check: verifies the secret is valid Base32 and
    generates a code without raising.
    """
    try:
        code = pyotp.TOTP(secret).now()
        return len(code) == 6 and code.isdigit()
    except Exception:
        return False


if __name__ == "__main__":
    secret = os.environ.get("ANGEL_TOTP_SECRET", "")
    if not secret:
        print("❌ ANGEL_TOTP_SECRET not set in config/secrets.env")
        sys.exit(1)

    if verify_totp_secret(secret):
        print(f"✅ TOTP secret is valid. Current code: {get_current_totp(secret)}")
    else:
        print("❌ TOTP secret appears invalid — check it's a Base32 string from Angel One.")
        sys.exit(1)
