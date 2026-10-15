#!/usr/bin/env sh
# ShieldLang launcher (macOS / Linux)
cd "$(dirname "$0")"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
echo "ShieldLang"
echo "  [1] Open ShieldLang Studio (web UI)"
echo "  [2] Compile examples/payments.shl (security report)"
echo "  [3] Run the test suite"
printf "Choose [1]: "; read c
case "$c" in
  2) $PY shieldlang.py compile examples/payments.shl --emit report ;;
  3) $PY shieldlang.py test ;;
  *) $PY shieldlang.py serve ;;
esac
