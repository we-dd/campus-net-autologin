import sys
from pathlib import Path

# 让 tests 里可以 `from src.xxx import yyy`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
