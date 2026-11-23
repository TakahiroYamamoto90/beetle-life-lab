"""Read-only collection snapshot including all labels and their append-only history."""
import argparse
from collections import Counter
import json
from pathlib import Path

from session_store import SessionStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("captures"))
    parser.add_argument("--output", type=Path, required=True, help="new JSONL file, never overwritten")
    args = parser.parse_args()
    rows = SessionStore(args.data_dir, readonly=True).classification_snapshot()
    with args.output.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"images": len(rows), "labels": Counter(row["label"] for row in rows),
                      "training_performed": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
