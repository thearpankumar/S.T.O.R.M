from pathlib import Path
from typing import Literal
import base64
import html

ICONS_DIR = Path(__file__).parent.parent / "assets" / "icons"

IconCategory = Literal["status", "actions", "navigation", "ui"]


def sanitize_for_html(text: str) -> str:
    """Escape HTML special characters to prevent XSS attacks."""
    return html.escape(str(text))


def load_svg(name: str, category: IconCategory = "actions") -> str:
    svg_path = ICONS_DIR / category / f"{name}.svg"
    if svg_path.exists():
        return svg_path.read_text(encoding="utf-8")
    return ""


def svg_to_base64(svg_content: str) -> str:
    encoded = base64.b64encode(svg_content.encode("utf-8")).decode("utf-8")
    return f"data:image/svg+xml;base64,{encoded}"


def icon(
    name: str,
    category: IconCategory = "actions",
    size: int = 20,
    color: str | None = None,
) -> str:
    svg = load_svg(name, category)
    if not svg:
        return ""
    
    if color:
        svg = svg.replace('stroke="currentColor"', f'stroke="{color}"')
        svg = svg.replace('fill="currentColor"', f'fill="{color}"')
    
    svg = svg.replace('width="20"', f'width="{size}"')
    svg = svg.replace('height="20"', f'height="{size}"')
    
    return svg


def icon_img(
    name: str,
    category: IconCategory = "actions",
    size: int = 20,
    color: str | None = None,
    alt: str = "",
) -> str:
    svg = icon(name, category, size, color)
    if not svg:
        return alt
    
    b64 = svg_to_base64(svg)
    return f'<img src="{b64}" alt="{alt}" style="width:{size}px;height:{size}px;vertical-align:middle;display:inline-block;">'


def icon_html(
    name: str,
    category: IconCategory = "actions",
    size: int = 20,
    color: str | None = None,
) -> str:
    svg = icon(name, category, size, color)
    if not svg:
        return ""
    return svg


STATUS_ICONS = {
    "pending": icon_html("pending", "status"),
    "running": icon_html("running", "status"),
    "done": icon_html("done", "status"),
    "failed": icon_html("failed", "status"),
}


def status_icon(status: str, size: int = 16) -> str:
    icons = {
        "pending": icon_html("pending", "status", size),
        "running": icon_html("running", "status", size),
        "done": icon_html("done", "status", size),
        "failed": icon_html("failed", "status", size),
    }
    return icons.get(status, icons["pending"])


ACTION_ICONS = {
    "search": icon_html("search", "actions"),
    "play": icon_html("play", "actions"),
    "stop": icon_html("stop", "actions"),
    "chart": icon_html("chart", "actions"),
    "trash": icon_html("trash", "actions"),
    "refresh": icon_html("refresh", "actions"),
    "export": icon_html("export", "actions"),
}


UI_ICONS = {
    "check": icon_html("check", "ui"),
    "close": icon_html("close", "ui"),
    "info": icon_html("info", "ui"),
    "warning": icon_html("warning", "ui"),
    "clock": icon_html("clock", "ui"),
    "file": icon_html("file", "ui"),
    "message": icon_html("message", "ui"),
    "checkbox": icon_html("checkbox", "ui"),
    "checkbox-checked": icon_html("checkbox-checked", "ui"),
}


NAV_ICONS = {
    "chevron-right": icon_html("chevron-right", "navigation"),
    "chevron-down": icon_html("chevron-down", "navigation"),
    "folder": icon_html("folder", "navigation"),
    "globe": icon_html("globe", "navigation"),
}

ACTION_ICONS["trending"] = icon_html("trending", "actions")
