#!/usr/bin/env python3
"""ShieldLang entry point — runs with plain Python 3.9+, no installation needed.

  python shieldlang.py                 # open ShieldLang Studio in the browser
  python shieldlang.py compile examples/payments.shl --emit report
  python shieldlang.py regex "^(a+)+$"
  python shieldlang.py test
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shieldlang.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
