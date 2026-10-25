"""Kalshi credentials, read from `settings.KALSHI_ENV_PATH` at CALL time, never at import."""
from kbtc import settings


def kalshi_credentials(path=None):
    env = {}
    with open(path or settings.KALSHI_ENV_PATH) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env["KALSHI_API_KEY_ID"], env["KALSHI_PRIVATE_KEY_PATH"]
