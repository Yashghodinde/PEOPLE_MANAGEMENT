#!/usr/bin/env python3
"""Build app/static/vendor/lucide-sprite.svg from a lucide-static checkout.
Usage: build_sprite.py <path to lucide-static/icons>. Only the icons listed in ICONS are included."""

import re
import sys
from pathlib import Path

ICONS = [
    "menu",
    "bell",
    "user",
    "users",
    "check",
    "x",
    "triangle-alert",
    "inbox",
    "upload",
    "clock",
    "camera",
    "calendar",
    "folder",
    "search",
    "log-out",
    "chevron-down",
    "chevron-right",
    "file",
    "tv",
    "settings",
    "layout-dashboard",
    "list-checks",
    "info",
    "circle-check",
    "circle-x",
    "plus",
    "download",
    "shield",
    "hard-drive",
    "activity",
    "history",
    "bar-chart-3",
    "panel-left",
    "send",
    "paperclip",
    "eye",
    "refresh-cw",
]


def main(src: Path) -> None:
    out = ['<svg xmlns="http://www.w3.org/2000/svg" style="display:none">']
    for name in ICONS:
        f = src / f"{name}.svg"
        if not f.exists():
            print(f"missing icon: {name}", file=sys.stderr)
            continue
        inner = re.search(r"<svg[^>]*>(.*)</svg>", f.read_text(), re.S)
        assert inner, name
        out.append(f'<symbol id="i-{name}" viewBox="0 0 24 24">{inner.group(1).strip()}</symbol>')
    out.append("</svg>")
    target = Path(__file__).resolve().parents[1] / "app/static/vendor/lucide-sprite.svg"
    target.write_text("\n".join(out) + "\n")
    print(f"wrote {target} ({len(out) - 2} icons)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
