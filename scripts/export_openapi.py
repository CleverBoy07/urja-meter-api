"""Write openapi.json from the FastAPI app (the code is the source of truth).

Usage: python scripts/export_openapi.py [output-path]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

spec = create_app(Settings(sync_on_startup=False)).openapi()
spec["servers"] = [{"url": "http://localhost:8000", "description": "Local development"}]
out = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
out.write_text(json.dumps(spec, indent=2) + "\n")
print(f"Wrote {out} ({len(spec['paths'])} paths)")
