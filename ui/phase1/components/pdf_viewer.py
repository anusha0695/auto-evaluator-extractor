"""
PDF viewer component — renders a PDF page as an image with optional bbox overlays.

Uses pdf2image (which uses poppler under the hood) to rasterize one page at a
time, then PIL.ImageDraw to draw colored rectangles + labels for each block.
"""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# A stable palette for text_role values. Anything not in this map gets a
# hash-derived color (still stable across runs).
_PALETTE: dict[str, str] = {
    "report_title":           "#7c3aed",   # purple
    "vendor_branding":        "#a855f7",
    "practice_block":         "#2563eb",   # blue
    "patient_demographics":   "#16a34a",   # green
    "specimen_metadata":      "#0d9488",   # teal
    "ordering_provider":      "#22c55e",
    "accession_block":        "#f97316",   # orange
    "panel_or_test_name":     "#8b5cf6",
    "methodology":            "#6b7280",   # gray
    "results_table":          "#dc2626",   # red — the most important block
    "interpretation":         "#ea580c",
    "clinical_significance":  "#ca8a04",   # amber
    "references":             "#9ca3af",
    "cpt_codes":              "#94a3b8",
    "electronic_signature":   "#475569",
    "page_header":            "#60a5fa",   # light blue
    "page_footer":            "#60a5fa",
    "fax_transport_noise":    "#ef4444",   # bright red — the filter target
    "chart_image_caption":    "#d1d5db",
    "disclaimer_or_notes":    "#e5e7eb",
    "other":                  "#9ca3af",
}


def color_for_role(role: str) -> str:
    """Hex color for a text_role. Falls back to a stable hash-derived color."""
    if role in _PALETTE:
        return _PALETTE[role]
    h = hashlib.md5(role.encode("utf-8")).digest()
    return f"#{h[0]:02x}{h[1]:02x}{h[2]:02x}"


def render_page_with_blocks(
    *,
    pdf_path: str | Path,
    page_number: int,
    blocks_with_profiles: list[dict[str, Any]] | None = None,
    dpi: int = 110,
) -> bytes | None:
    """Render page `page_number` of `pdf_path` as PNG bytes. Overlay each
    block's bbox if `blocks_with_profiles` provided.

    Returns None if the file doesn't exist or pdf2image fails (e.g. poppler
    not installed — `brew install poppler` on macOS).
    """
    p = Path(pdf_path)
    if not p.exists():
        return None

    try:
        from pdf2image import convert_from_path
    except ImportError:
        logger.warning("pdf2image not installed — PDF viewer disabled")
        return None

    try:
        images = convert_from_path(
            str(p), first_page=page_number, last_page=page_number, dpi=dpi
        )
    except Exception as exc:
        logger.warning("pdf2image failed for page %d of %s: %s", page_number, p, exc)
        return None
    if not images:
        return None

    img = images[0].convert("RGBA")
    w, h = img.size

    if blocks_with_profiles:
        from PIL import Image, ImageDraw, ImageFont

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        try:
            font = ImageFont.truetype("Helvetica", 11)
        except Exception:
            font = ImageFont.load_default()

        for entry in blocks_with_profiles:
            bbox = entry.get("bbox") or []
            if len(bbox) < 4:
                continue
            x0, y0, x1, y1 = bbox[:4]
            # DocAI bboxes are in normalized [0..1] coords.
            px = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
            role = entry.get("text_role") or "other"
            color_hex = color_for_role(role)
            # Hex → RGBA
            r, g, b = int(color_hex[1:3], 16), int(color_hex[3:5], 16), int(color_hex[5:7], 16)
            fill = (r, g, b, 38)        # 15% opacity
            outline = (r, g, b, 200)    # ~80% opacity

            draw.rectangle(px, fill=fill, outline=outline, width=2)
            # Tiny label in the top-left of each bbox
            label = role[:24]
            tx, ty = px[0] + 2, max(0, px[1] - 13)
            # Solid background behind the label so it's readable
            tw = draw.textlength(label, font=font)
            draw.rectangle((tx - 1, ty, tx + tw + 2, ty + 12), fill=(r, g, b, 230))
            draw.text((tx, ty), label, fill=(255, 255, 255, 255), font=font)

        img = Image.alpha_composite(img, overlay)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
