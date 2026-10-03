"""Record acceptance only after the stage's checks have passed."""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("stage", type=int)
parser.add_argument("evidence")
args = parser.parse_args()
digits = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
path = Path(__file__).with_name("CHECKLIST.md")
text = path.read_text(encoding="utf-8")
start = text.index("## مرحلهٔ " + str(args.stage).translate(digits) + " —")
end = text.find("\n## ", start + 1)
if end < 0:
    end = len(text)
section = text[start:end].replace("- [ ]", "- [x]")
section += "\n**شواهد پذیرش:** " + args.evidence + "\n"
path.write_text(text[:start] + section + text[end:], encoding="utf-8")
