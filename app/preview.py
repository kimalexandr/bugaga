"""Рендер превью страниц PDF."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def render_pdf_preview(
    pdf_path: Path,
    page: int,
    output: Path,
    *,
    max_width: int = 900,
    max_height: int = 500,
) -> bool:
    if _render_pymupdf(pdf_path, page, output, max_width, max_height):
        return True
    if _render_pdftoppm(pdf_path, page, output, max_width, max_height):
        return True
    return False


def _render_pymupdf(pdf_path: Path, page: int, output: Path, max_w: int, max_h: int) -> bool:
    try:
        import fitz
    except ImportError:
        return False
    try:
        doc = fitz.open(str(pdf_path))
        if page < 1 or page > doc.page_count:
            doc.close()
            return False
        pg = doc[page - 1]
        rect = pg.rect
        if rect.width <= 0 or rect.height <= 0:
            doc.close()
            return False
        scale = min(max_w / rect.width, max_h / rect.height) * 1.5
        pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(output))
        doc.close()
        return output.is_file() and output.stat().st_size > 500
    except Exception as e:
        logger.warning("PyMuPDF p%s: %s", page, e)
        return False


def _render_pdftoppm(pdf_path: Path, page: int, output: Path, max_w: int, max_h: int) -> bool:
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        prefix = output.with_suffix("")
        cmd = [
            "pdftoppm",
            "-png",
            "-f",
            str(page),
            "-l",
            str(page),
            "-scale-to-x",
            str(max_w),
            "-scale-to-y",
            str(max_h),
            str(pdf_path),
            str(prefix),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return False
        for candidate in sorted(output.parent.glob(f"{prefix.name}*.png")):
            if candidate.stat().st_size > 500:
                candidate.replace(output)
                return True
        direct = Path(f"{prefix}-{page:02d}.png")
        if direct.is_file():
            direct.replace(output)
            return True
        return output.is_file() and output.stat().st_size > 500
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning("pdftoppm p%s: %s", page, e)
        return False
