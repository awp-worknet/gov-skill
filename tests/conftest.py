"""Add scripts/lib to sys.path so `from lib.canonical import ...` works directly."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
