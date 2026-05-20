"""
page_render — rasterize PDF pages to base64 PNGs for the explorer overlay (U4).

Uses pdf2image (poppler). Each page is rendered at a fixed DPI and returned as
a data: URI plus its pixel dimensions, so the front-end can map normalized
DocAI bboxes ([x0,y0,x1,y1] in 0..1) onto the rendered image.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# DPI + JPEG keep the inline base64 payload small. The page image is only a
# visual reference behind the bbox overlays, so JPEG q75 at 110 DPI is plenty
# legible while cutting each page from ~2.8 MB (PNG@150) to ~200 KB.
_DEFAULT_DPI = 130
_JPEG_QUALITY = 85


def render_pages(
    pdf_path: Path, *, dpi: int = _DEFAULT_DPI, jpeg_quality: int = _JPEG_QUALITY,
) -> list[dict]:
    """Return one dict per page: {page_number, width, height, data_uri}.

    Pages are encoded as JPEG to keep the inline base64 payload small (the
    explorer ships these into an iframe). Returns [] if pdf2image/poppler is
    unavailable or the file is missing — the explorer then falls back to a
    blank canvas with overlays only.
    """
    if not pdf_path or not Path(pdf_path).exists():
        logger.warning("page_render: PDF not found at %s", pdf_path)
        return []
    try:
        from pdf2image import convert_from_path
    except ImportError:
        logger.warning("page_render: pdf2image not installed — no page images.")
        return []

    try:
        images = convert_from_path(str(pdf_path), dpi=dpi)
    except Exception as exc:
        logger.warning("page_render: convert_from_path failed (%s). "
                       "Is poppler installed?", exc)
        return []

    out: list[dict] = []
    for i, img in enumerate(images, start=1):
        buf = io.BytesIO()
        # JPEG has no alpha; flatten to RGB to avoid mode errors.
        img.convert("RGB").save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        out.append({
            "page_number": i,
            "width": img.width,
            "height": img.height,
            "data_uri": f"data:image/jpeg;base64,{b64}",
        })
    return out
