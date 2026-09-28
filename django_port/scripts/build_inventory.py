"""Build a reviewable index of the ERPNext DocTypes in the sibling source tree."""

import csv
import json
from pathlib import Path


PORT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = PORT_ROOT.parent / "erpnext"
OUTPUT = PORT_ROOT / "source_inventory.csv"
RELATION_TYPES = {"Link", "Table", "Table MultiSelect"}
PROGRESS = json.loads((PORT_ROOT / "port_progress.json").read_text(encoding="utf-8"))["artifacts"]


def main():
    rows = []
    for path in SOURCE_ROOT.glob("*/doctype/*/*.json"):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict) or document.get("doctype") != "DocType":
            continue

        fields = document.get("fields", [])
        relations = sorted(
            {
                f["options"]
                for f in fields
                if isinstance(f, dict)
                and f.get("fieldtype") in RELATION_TYPES
                and isinstance(f.get("options"), str)
                and f["options"]
            }
        )
        rows.append(
            {
                "module": document.get("module", path.parts[-4]),
                "doctype": document["name"],
                "field_count": len(fields),
                "relations": "; ".join(relations),
                "is_child_table": int(bool(document.get("istable"))),
                "is_submittable": int(bool(document.get("is_submittable"))),
                "status": (
                    "complete"
                    if PROGRESS.get(f"DocType:{document['name']}", {}).get("complete")
                    else "partial"
                    if PROGRESS.get(f"DocType:{document['name']}", {}).get("done")
                    else "not_started"
                ),
                "source": path.relative_to(PORT_ROOT.parent).as_posix(),
            }
        )

    rows.sort(key=lambda row: (row["module"], row["doctype"]))
    with OUTPUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Indexed {len(rows)} DocTypes in {OUTPUT}")


if __name__ == "__main__":
    main()
