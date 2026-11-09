#!/usr/bin/env python3
"""Template lint run by `make lint` (M01). Fails on: rounded-none, hex colours outside the token list,
inline-style colours, external (http/https) asset URLs, and (M13) inline <script> or on* handlers."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = {v.lower() for v in json.loads((ROOT / "app/static_src/tokens.json").read_text())["colors"].values()}
HEX = re.compile(r"(?<![\w&/])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![\w-])")
EXTERNAL = re.compile(
    r"""(?:src|href|action|data-src)\s*=\s*["']https?://|url\(\s*["']?https?://|@import\s+["']?https?://""",
    re.I,
)
STYLE_COLOR = re.compile(r"""style\s*=\s*["'][^"']*(?:color|background|border-color|fill|stroke)\s*:""", re.I)
INLINE_JS = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(?!\s*</script>)|\son[a-z]+\s*=\s*[\"']", re.I)
FILES = list((ROOT / "app/templates").rglob("*.html")) + list((ROOT / "app/ots").rglob("templates/**/*.html"))
FILES += [ROOT / "app/static_src/app.css"]


def check(path: Path) -> list[str]:
    problems = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        rel = f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.name}:{n}"
        if "rounded-none" in line:
            problems.append(f"{rel}: rounded-none is not allowed")
        for m in HEX.finditer(line):
            if m.group(0).lower() not in TOKENS and path.suffix == ".html":
                problems.append(f"{rel}: colour {m.group(0)} is not a design token")
        if EXTERNAL.search(line):
            problems.append(f"{rel}: external asset URL (no CDN)")
        if STYLE_COLOR.search(line):
            problems.append(f"{rel}: inline style colour")
        if path.suffix == ".html" and INLINE_JS.search(line):
            problems.append(f"{rel}: inline <script> or on*= handler (CSP)")
    return problems


def main() -> int:
    problems = [p for f in FILES if f.exists() for p in check(f)]
    for p in problems:
        print(p, file=sys.stderr)
    print(f"check_templates: {len(FILES)} files, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
