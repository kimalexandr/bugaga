"""Рендер превью страниц PDF (fallback если Callas saveasimg не сработал)."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def render_pdf_preview(
    pdf_path: Path,
    page: int,
    output: Path,
    *,
    max_width: int = 800,
    max_height: int = 440,
) -> bool:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF не установлен — превью недоступно")
        return False

    try:
        doc = fitz.open(pdf_path)
        if page < 1 or page > doc.page_count:
            doc.close()
            return False

        pg = doc[page - 1]
        rect = pg.rect
        if rect.width <= 0 or rect.height <= 0:
            doc.close()
            return False

        scale = min(max_width / rect.width, max_height / rect.height)
        matrix = fitz.Matrix(scale, scale)
        pix = pg.get_pixmap(matrix=matrix, alpha=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(output))
        doc.close()
        return output.is_file() and output.stat().st_size > 200
    except Exception as e:
        logger.warning("render_pdf_preview p%s: %s", page, e)
        return False
