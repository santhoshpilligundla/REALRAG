import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.business_docs import build_business_index

n = build_business_index()
print(f"business-doc chunks indexed: {n}")
