"""Write the API's OpenAPI schema to `openapi.json` (sorted keys, so diffs stay readable).

Usage: uv run python -m scripts.export_openapi
The web app generates its typed API client from this file, so commit it with every API change.
"""

import json
from pathlib import Path

from app.main import app

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "openapi.json"


def export_openapi(path: Path = OUTPUT_PATH) -> None:
    """Serialise the schema deterministically and write it to disk."""
    schema = app.openapi()
    text = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {path}")


if __name__ == "__main__":
    export_openapi()
