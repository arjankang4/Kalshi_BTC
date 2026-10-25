"""kbtc: quoting Kalshi BTC event markets (KXBTC15M / KXBTCD / KXBTC) off the KXBTCPERP book.

Separate from the companion `Kalshi` repo on purpose: that tree's root `config` is equity-specific (SPX/NDX, the
09:30-16:00 session) and loads secrets at import. kbtc imports only its config-free modules,
via `kbtc.kalshi_root`, and `tests.test_import_guard` holds that line.
"""
