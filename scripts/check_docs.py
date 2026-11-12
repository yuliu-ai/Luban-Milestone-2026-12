"""Check local Markdown targets without a network or documentation server."""

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures = []
    pages = list(ROOT.glob("*.md"))
    for directory in (
        "docs",
        "impl_docs",
        "catalogs",
        "configs",
        "rules",
        "benchmarks",
        "ci",
        ".github",
    ):
        pages.extend((ROOT / directory).rglob("*.md"))
    pages.sort()
    for page in pages:
        source = re.sub(r"```.*?```", "", page.read_text(), flags=re.S)
        targets = re.findall(r"\[[^\]]*\]\(([^)]+)\)", source)
        targets += re.findall(r'(?:href|src)="([^"]+)"', source)
        for value in targets:
            target = value.split("#", 1)[0].strip().strip("<>")
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if not (page.parent / unquote(target)).exists():
                failures.append(f"{page.relative_to(ROOT)}: {target}")
    guides = [
        p
        for p in (ROOT / "docs").glob("*.md")
        if p.name != "_sidebar.md" and not p.name.endswith(".zh-CN.md")
    ]
    for english in guides:
        chinese = english.with_name(english.stem + ".zh-CN.md")
        if not chinese.is_file():
            failures.append(f"Missing Chinese guide: {chinese.name}")
            continue
        en = english.read_text()
        zh = chinese.read_text()
        if len(re.findall(r"[\u4e00-\u9fff]", zh)) < 30:
            failures.append(f"Chinese guide lacks content: {chinese.name}")
        if f"[English]({english.name})" not in zh:
            failures.append(f"Missing English switch: {chinese.name}")
        if f"[简体中文]({chinese.name})" not in en:
            failures.append(f"Missing Chinese switch: {english.name}")
        for target in re.findall(r"\[English(?: overview)?\]\(([^)]+)\)", zh):
            if target.endswith(".zh-CN.md"):
                failures.append(f"English link targets Chinese page: {chinese.name}")
        blocks = r"```[^\n]*\n.*?```"
        if re.findall(blocks, en, re.S) != re.findall(blocks, zh, re.S):
            failures.append(f"EN/ZH code blocks differ: {english.name}")
    for failure in failures:
        print(failure)
    print(f"Local Markdown targets: {len(pages)} pages, {len(failures)} failures")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
