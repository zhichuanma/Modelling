"""
Pytest bootstrap: put the Modelling repo root on sys.path so tests can `from mobility.xxx import yyy`.

This replaces the per-test sys.path.insert + del sys.modules['mobility'] hacks
that were added in Stage 0. Keeping path manipulation in one place avoids
cross-test module-cache pollution when multiple stage sub-packages are
collected together.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
