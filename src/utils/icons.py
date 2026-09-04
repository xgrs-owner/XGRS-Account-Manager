"""
utils/icons.py
Vector (SVG) icons for the navigation tabs and circular-icon helpers.

The icons are inline SVG sources rendered on demand with QSvgRenderer, so they
stay sharp on every DPI and can be recolored without shipping image files.
Icon shapes follow the Feather Icons set (MIT licensed).
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer

_SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{color}" stroke-width="{width}" stroke-linecap="round" '
    'stroke-linejoin="round">{body}</svg>'
)

# Icon body per navigation tab
_ICON_BODIES: dict[str, str] = {
    "accounts": (
        '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
        '<circle cx="9" cy="7" r="4"/>'
        '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>'
        '<path d="M16 3.13a4 4 0 0 1 0 7.75"/>'
    ),
    "auto_rejoin": (
        '<polyline points="23 4 23 10 17 10"/>'
        '<polyline points="1 20 1 14 7 14"/>'
        '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/>'
        '<path d="M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>'
    ),
    "auto_connect": (
        '<path d="M6 2v6a6 6 0 0 0 12 0V2"/>'
        '<line x1="9" y1="2" x2="9" y2="6"/>'
        '<line x1="15" y1="2" x2="15" y2="6"/>'
        '<line x1="12" y1="14" x2="12" y2="22"/>'
    ),
    "anti_afk": (
        '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>'
    ),
    "multi_roblox": (
        '<polygon points="12 2 2 7 12 12 22 7 12 2"/>'
        '<polyline points="2 17 12 22 22 17"/>'
        '<polyline points="2 12 12 17 22 12"/>'
    ),
    "settings": (
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83'
        'l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0'
        'v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83'
        'l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09'
        'A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83'
        'l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09'
        'a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83'
        'l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09'
        'a1.65 1.65 0 0 0-1.51 1z"/>'
    ),
    "console": (
        '<polyline points="4 17 10 11 4 5"/>'
        '<line x1="12" y1="19" x2="20" y2="19"/>'
    ),
    "setup": (
        '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>'
    ),
    # Settings categories
    "roblox": (
        '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8'
        'a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>'
        '<polyline points="3.27 6.96 12 12.01 20.73 6.96"/>'
        '<line x1="12" y1="22.08" x2="12" y2="12"/>'
    ),
    "discord": (
        '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'
    ),
    "misc": (
        '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/>'
        '<line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/>'
        '<line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/>'
        '<line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/>'
        '<line x1="17" y1="16" x2="23" y2="16"/>'
    ),
    "developer": (
        '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>'
    ),
    "themes": (
        '<path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/>'
    ),
    "search": (
        '<circle cx="11" cy="11" r="8"/>'
        '<line x1="21" y1="21" x2="16.65" y2="16.65"/>'
    ),
    # Window controls
    "skull": (
        '<path d="M12 2a8 8 0 0 0-8 8v3.2a2 2 0 0 0 1.1 1.8L6 15.4V19a1 1 0 0 0 1 1h10'
        'a1 1 0 0 0 1-1v-3.6l.9-.4a2 2 0 0 0 1.1-1.8V10a8 8 0 0 0-8-8z"/>'
        '<circle cx="9" cy="10.5" r="1.6"/><circle cx="15" cy="10.5" r="1.6"/>'
        '<path d="M10.5 15.5h3"/><path d="M10 20v-2"/><path d="M14 20v-2"/>'
    ),
    "minimize": (
        '<line x1="5" y1="12" x2="19" y2="12"/>'
    ),
    "maximize": (
        '<rect x="4" y="4" width="16" height="16" rx="2"/>'
    ),
    "restore": (
        '<rect x="3" y="8" width="13" height="13" rx="2"/>'
        '<path d="M8 8V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-3"/>'
    ),
    "close": (
        '<line x1="18" y1="6" x2="6" y2="18"/>'
        '<line x1="6" y1="6" x2="18" y2="18"/>'
    ),
}

_ICON_CACHE: dict[tuple[str, str, int, float], QIcon] = {}


def render_svg_pixmap(svg: str, size: int) -> QPixmap:
    """Rasterize an SVG source string into a transparent pixmap."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(svg.encode("utf-8"))
    if not renderer.isValid():
        return pixmap
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def get_icon(name: str, color: str, size: int = 16, stroke_width: float = 2.0) -> QIcon:
    """Return a cached, recolored vector icon for a navigation tab."""
    body = _ICON_BODIES.get(name)
    if not body:
        return QIcon()

    key = (name, color, size, stroke_width)
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached

    svg = _SVG_TEMPLATE.format(color=color, width=stroke_width, body=body)
    icon = QIcon(render_svg_pixmap(svg, size))
    _ICON_CACHE[key] = icon
    return icon


def make_circular_pixmap(source: QPixmap, size: int) -> QPixmap:
    """Crop a pixmap to a circle, scaling it to `size` first."""
    if source.isNull():
        return QPixmap()

    scaled = source.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)

    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    path = QPainterPath()
    path.addEllipse(0.0, 0.0, float(size), float(size))
    painter.setClipPath(path)
    painter.drawPixmap(
        -(scaled.width() - size) // 2,
        -(scaled.height() - size) // 2,
        scaled,
    )
    painter.end()
    return result


def make_circular_icon(icon_path: str, sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)) -> QIcon:
    """
    Build a round application icon from the packaged square icon file.
    Returns an empty icon when the source cannot be read.
    """
    if not icon_path:
        return QIcon()

    source_icon = QIcon(icon_path)
    largest = source_icon.pixmap(QSize(256, 256))
    if largest.isNull():
        largest = QPixmap(icon_path)
    if largest.isNull():
        return QIcon()

    result = QIcon()
    for size in sizes:
        result.addPixmap(make_circular_pixmap(largest, size))
    return result
