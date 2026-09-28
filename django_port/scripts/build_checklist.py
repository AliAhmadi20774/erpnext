"""Generate the full ERPNext artifact checklist from source and recorded progress."""

import json
from collections import defaultdict
from pathlib import Path


PORT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = PORT_ROOT.parent / "erpnext"
PROGRESS_FILE = PORT_ROOT / "port_progress.json"
OUTPUT_FILE = PORT_ROOT / "CHECKLIST.md"

ARTIFACT_TYPES = (
    ("doctype", "DocType", "DocTypeها"),
    ("report", "Report", "گزارش‌ها"),
    ("print_format", "Print Format", "قالب‌های چاپ"),
    ("page", "Page", "صفحه‌ها"),
    ("workspace", "Workspace", "فضاهای کاری"),
    ("dashboard_chart", "Dashboard Chart", "نمودارهای داشبورد"),
    ("number_card", "Number Card", "کارت‌های عددی"),
    ("web_form", "Web Form", "فرم‌های وب"),
)

FRAMEWORK_TASKS = (
    "کاربران، نقش‌ها و مجوزهای سندی Frappe",
    "چرخهٔ عمر سند، ثبت نهایی و لغو سند",
    "قواعد نام‌گذاری و تغییر نام سندها",
    "گردش‌کارها و تأییدها",
    "فایل‌های پیوست، دیدگاه‌ها و ثبت تغییرات",
    "ترجمه و تنظیمات زبان و منطقه",
    "API و احراز هویت",
    "کارهای زمان‌بندی‌شده و صف پردازش",
    "ایمیل و اعلان‌ها",
    "مهاجرت داده از پایگاه دادهٔ Frappe",
    "آزمون‌های تطبیقی و پذیرش برای کل سیستم",
)


def collect(folder, expected_type):
    artifacts = defaultdict(list)
    for path in SOURCE_ROOT.glob(f"*/{folder}/*/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("doctype") != expected_type:
            continue
        module = data.get("module") or path.parts[-4]
        artifacts[module].append((data["name"], path.relative_to(PORT_ROOT.parent)))
    return artifacts


def checkbox(done):
    return "[x]" if done else "[ ]"


def main():
    progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    statuses = progress["artifacts"]
    lines = [
        "# چک‌لیست انتقال ERPNext به Django",
        "",
        "این فهرست از تعریف‌های همین مخزن ساخته شده است. تیک کنار یک قابلیت فقط وقتی زده می‌شود که انتقال کامل آن طبق معیارهای [PORTING.md](PORTING.md) بررسی شده باشد. کارهای جزئیِ انجام‌شده در بخش پیشرفت تیک دارند.",
        "",
        "## زیرساخت انجام‌شده",
        "",
    ]
    lines.extend(f"- [x] {item}" for item in progress["project_done"])
    lines.extend(["", "## پیشرفت قابلیت‌های شروع‌شده", ""])
    for key in sorted(statuses):
        status = statuses[key]
        if status.get("complete") and status.get("remaining"):
            raise ValueError(f"{key} is marked complete but still has remaining work")
        lines.append(f"### {key.removeprefix('DocType:')}")
        lines.append("")
        lines.extend(f"- [x] {item}" for item in status.get("done", []))
        lines.extend(f"- [ ] {item}" for item in status.get("remaining", []))
        lines.append("")

    lines.extend(["## زیرساخت مشترک Frappe", ""])
    lines.extend(f"- [ ] {item}" for item in FRAMEWORK_TASKS)
    lines.append("")
    lines.extend(["## تعریف‌های موجود در Frappe", ""])
    lines.append("Country و Currency در مخزن Frappe تعریف شده‌اند و در شمارش فایل‌های ERPNext نیستند.")
    lines.append("")
    lines.append("Address، Contact، Contact Email، Contact Phone و Dynamic Link نیز در Frappe تعریف شده‌اند و در شمارش ERPNext نیستند.")
    lines.append("")
    for name in ("Country", "Currency", "Address", "Contact", "Contact Email", "Contact Phone", "Dynamic Link"):
        status = statuses[f"DocType:{name}"]
        lines.append(f"- {checkbox(status['complete'])} {name} — پیشرفت جزئی در بخش بالا")
    lines.append("")

    total = 0
    for folder, expected_type, heading in ARTIFACT_TYPES:
        artifacts = collect(folder, expected_type)
        count = sum(map(len, artifacts.values()))
        total += count
        lines.extend([f"## {heading} ({count})", ""])
        for module in sorted(artifacts):
            entries = sorted(artifacts[module], key=lambda item: item[0])
            lines.extend([f"### {module} ({len(entries)})", ""])
            for name, source in entries:
                key = f"{expected_type}:{name}"
                status = statuses.get(key, {})
                suffix = " — پیشرفت جزئی در بخش بالا" if status.get("done") and not status.get("complete") else ""
                lines.append(
                    f"- {checkbox(status.get('complete', False))} "
                    f"[{name}](<../{source.as_posix()}>) {suffix}".rstrip()
                )
            lines.append("")

    lines[2] += f" شمار تعریف‌های ERPNext در این نسخه: **{total}**."
    OUTPUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {total} ERPNext artifacts to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
