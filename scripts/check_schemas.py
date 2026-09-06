"""Keep editor schemas and runtime schemas identical, without dependencies."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sdlc.schema import exported_schema
from sdlc.workspace import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    for kind in ("change", "config", "receipt"):
        path = ROOT / "schemas" / f"{kind}.schema.json"
        expected = exported_schema(kind)
        if args.write:
            write_json(path, expected)
        elif not path.exists() or json.loads(path.read_text()) != expected:
            raise SystemExit(f"Schema drift: {path}; run make schemas")
    print("All three editor schemas match runtime validation.")


if __name__ == "__main__":
    main()
