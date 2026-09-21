"""
auth/session_manager.py

Handles headless Angel One login via SmartAPI.
- Uses pyotp for TOTP (no browser needed)
- Retries on transient failures
- Exposes the authenticated SmartConnect object and tokens
- Schedule a fresh login daily before 9:00 AM IST (tokens expire at 5 AM next day)

Usage (standalone test):
    python -m auth.session_manager
"""

from __future__ import annotations
import os, time, logging
from pathlib import Path
from dotenv import load_dotenv

import pyotp

# Load secrets.env relative to project root
_ENV_PATH = Path(__file__).parent.parent / "config" / "secrets.env"
load_dotenv(_ENV_PATH)

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages the Angel One SmartAPI session lifecycle.

    Attributes:
        obj          : authenticated SmartConnect instance
        jwt_token    : JWT for REST API calls
        refresh_token: for token renewal (rarely needed; prefer fresh login)
        feed_token   : required for SmartWebSocketV2 subscription
    """

    def __init__(self) -> None:
        self.api_key       = os.environ["ANGEL_API_KEY"]
        self.client_id     = os.environ["ANGEL_CLIENT_ID"]
        self.password      = os.environ["ANGEL_PASSWORD"]
        self.totp_secret   = os.environ["ANGEL_TOTP_SECRET"]

        # Lazy import — so tests that don't need the live SDK can mock it
        from SmartApi import SmartConnect  # type: ignore
        self.obj           = SmartConnect(api_key=self.api_key)
        self.jwt_token     = None
        self.refresh_token = None
        self.feed_token    = None
        self._logged_in    = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def login(self, retries: int = 5, backoff: float = 2.0) -> "SmartConnect":
        """
        Perform a fresh session login.
        Returns the authenticated SmartConnect object.
        Raises RuntimeError if all retries are exhausted.
        """
        totp = pyotp.TOTP(self.totp_secret)

        for attempt in range(1, retries + 1):
            try:
                otp = totp.now()
                logger.info(f"[Auth] Login attempt {attempt}/{retries} ...")
                data = self.obj.generateSession(self.client_id, self.password, otp)

                if data and data.get("status"):
                    self.jwt_token     = data["data"]["jwtToken"]
                    self.refresh_token = data["data"]["refreshToken"]
                    self.feed_token    = self.obj.getfeedToken()
                    self._logged_in    = True
                    logger.info("[Auth] Login successful. Feed token obtained.")
                    return self.obj

                # Non-exception failure (API returned status=False)
                error_msg = data.get("message", "Unknown error") if data else "Empty response"
                logger.warning(f"[Auth] Login failed: {error_msg}. Retrying in {backoff}s ...")

            except Exception as exc:
                logger.warning(f"[Auth] Exception on attempt {attempt}: {exc}. Retrying in {backoff}s ...")

            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)   # Exponential backoff, capped at 30s

        raise RuntimeError(
            f"SmartAPI login failed after {retries} attempts. "
            "Check credentials, TOTP secret, and network connectivity."
        )

    def logout(self) -> None:
        """Terminate the session gracefully."""
        if self._logged_in:
            try:
                self.obj.terminateSession(self.client_id)
                logger.info("[Auth] Session terminated.")
            except Exception as exc:
                logger.warning(f"[Auth] Logout error (non-critical): {exc}")
            finally:
                self._logged_in = False

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    def get_tokens(self) -> dict:
        """Return current tokens as a dict (useful for passing to WebSocket)."""
        if not self._logged_in:
            raise RuntimeError("Not logged in. Call login() first.")
        return {
            "jwt_token"    : self.jwt_token,
            "refresh_token": self.refresh_token,
            "feed_token"   : self.feed_token,
            "api_key"      : self.api_key,
            "client_id"    : self.client_id,
        }


# ------------------------------------------------------------------
# Standalone test entry point
# ------------------------------------------------------------------
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")

    sm = SessionManager()
    sm.login()

    tokens = sm.get_tokens()
    print("\n✅ Login successful! Tokens:")
    print(json.dumps({k: v[:20] + "..." if v and len(str(v)) > 20 else v
                      for k, v in tokens.items()}, indent=2))
    sm.logout()
