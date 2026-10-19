"""A small SVG badge with the mutation score, in the style of shields.io, for a README."""

from __future__ import annotations

import hashlib
from html import escape

COLORS = ((90, "#2e7d32"), (75, "#7cb342"), (60, "#f9a825"), (0, "#c62828"))


def color_for(score: float | None) -> str:
    if score is None:
        return "#757575"
    return next(color for limit, color in COLORS if score >= limit)


def _width(text: str) -> int:
    """Rough text width in pixels for 11px Verdana (what shields.io uses)."""
    narrow = sum(text.count(c) for c in "il.:;,|!' ")
    return int(round(7.4 * (len(text) - narrow) + 3.5 * narrow)) + 12


def render_badge(score: float | None, label: str = "mutation score") -> str:
    """SVG text for a badge such as ``mutation score | 87.5%``."""
    value = "unknown" if score is None else f"{score:.1f}%"
    left, right = _width(label), _width(value)
    total = left + right
    label_x, value_x = left / 2, left + right / 2
    title = escape(f"{label}: {value}")
    label_text, value_text = escape(label), escape(value)
    uid = hashlib.sha1(f"{label}|{value}".encode("utf-8")).hexdigest()[:8]  # stable ids, unique per badge
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{title}">'
        f"<title>{title}</title>"
        f'<linearGradient id="s{uid}" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        '<stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<clipPath id="r{uid}"><rect width="{total}" height="20" rx="3" fill="#fff"/></clipPath>'
        f'<g clip-path="url(#r{uid})"><rect width="{left}" height="20" fill="#555"/>'
        f'<rect x="{left}" width="{right}" height="20" fill="{color_for(score)}"/>'
        f'<rect width="{total}" height="20" fill="url(#s{uid})"/></g>'
        '<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{label_x}" y="15" fill="#010101" fill-opacity=".3">{label_text}</text>'
        f'<text x="{label_x}" y="14">{label_text}</text>'
        f'<text x="{value_x}" y="15" fill="#010101" fill-opacity=".3">{value_text}</text>'
        f'<text x="{value_x}" y="14">{value_text}</text></g></svg>\n'
    )
