"""把 scripts/lib 加到 sys.path，让 `from lib.canonical import ...` 直接可用。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
