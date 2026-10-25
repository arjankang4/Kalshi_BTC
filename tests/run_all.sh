#!/usr/bin/env bash
# Run every kbtc test module; exit non-zero if any fails.
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-python}
rc=0
for f in tests/test_*.py; do
  m=tests.$(basename "$f" .py)
  if $PY -m "$m" > "${TMPDIR:-/tmp}/$m.out" 2>&1; then echo "PASS $m"; else echo "FAIL $m"; tail -20 "${TMPDIR:-/tmp}/$m.out"; rc=1; fi
done
exit $rc
