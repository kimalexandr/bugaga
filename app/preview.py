"""Рендер превью страниц PDF."""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MM_TO_PT = 72.0 / 25.4
DEFAULT_TRIM_PREVIEW_OFFSET_MM = float(os.getenv("TRIM_PREVIEW_OFFSET_MM", "1.0"))


def render_pdf_preview(
    pdf_path: Path,
    page: int,
    output: Path,
    *,
    max_width: int = 900,
    max_height: int = 500,
    pagebox: str = "mediabox",
) -> bool:
    if _render_pymupdf(pdf_path, page, output, max_width, max_height, pagebox=pagebox):
        return True
    if _render_pdftoppm(pdf_path, page, output, max_width, max_height):
        return True
    return False


def render_markup_preview(
    pdf_path: Path,
    page: int,
    output: Path,
    *,
    safe_mm: float = 2.0,
    trim_offset_mm: float = DEFAULT_TRIM_PREVIEW_OFFSET_MM,
    max_width: int = 900,
    max_height: int = 500,
) -> bool:
    """MediaBox + линия реза (со смещением для допуска обрезки) и safe-зона."""
    return _render_markup_pil(
        pdf_path,
        page,
        output,
        safe_mm=safe_mm,
        trim_offset_mm=trim_offset_mm,
        max_width=max_width,
        max_height=max_height,
    )


def _trim_preview_rect(trim, offset_mm: float):
    """Смещение линии реза вниз-вправо на экране (допуск гильотины ~1 мм)."""
    import fitz

    if offset_mm <= 0:
        return trim
    off = offset_mm * MM_TO_PT
    return fitz.Rect(trim.x0 + off, trim.y0 - off, trim.x1 + off, trim.y1 - off)


def _render_markup_pil(
    pdf_path: Path,
    page: int,
    output: Path,
    *,
    safe_mm: float,
    trim_offset_mm: float,
    max_width: int,
    max_height: int,
) -> bool:
    try:
        import fitz
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    try:
        doc = fitz.open(str(pdf_path))
        pg = doc[page - 1]
        media = pg.mediabox
        trim = pg.trimbox if pg.trimbox else media
        scale = min(max_width / media.width, max_height / media.height)
        mat = fitz.Matrix(scale, scale)
        pix = pg.get_pixmap(matrix=mat, clip=media, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        draw = ImageDraw.Draw(img)

        def px(rect: fitz.Rect) -> tuple[int, int, int, int]:
            return (
                int((rect.x0 - media.x0) * scale),
                int((rect.y0 - media.y0) * scale),
                int((rect.x1 - media.x0) * scale),
                int((rect.y1 - media.y0) * scale),
            )

        safe_pt = safe_mm * 72.0 / 25.4
        trim_show = _trim_preview_rect(trim, trim_offset_mm)
        safe = fitz.Rect(
            trim_show.x0 + safe_pt,
            trim_show.y0 + safe_pt,
            trim_show.x1 - safe_pt,
            trim_show.y1 - safe_pt,
        )
        if trim_offset_mm > 0.05:
            _dashed_rect(draw, px(trim), (180, 180, 180), 1)
        _dashed_rect(draw, px(trim_show), (76, 175, 80), 2)
        _dashed_rect(draw, px(safe), (244, 67, 54), 2)

        output.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(output), "PNG")
        doc.close()
        return output.stat().st_size > 500
    except Exception as e:
        logger.warning("markup PIL p%s: %s", page, e)
        return False


def _dashed_rect(draw, box: tuple[int, int, int, int], color: tuple[int, int, int], width: int) -> None:
    x0, y0, x1, y1 = box
    dash, gap = 8, 5
    for x in range(x0, x1, dash + gap):
        draw.line([(x, y0), (min(x + dash, x1), y0)], fill=color, width=width)
        draw.line([(x, y1), (min(x + dash, x1), y1)], fill=color, width=width)
    for y in range(y0, y1, dash + gap):
        draw.line([(x0, y), (x0, min(y + dash, y1))], fill=color, width=width)
        draw.line([(x1, y), (x1, min(y + dash, y1))], fill=color, width=width)


def _render_pymupdf(
    pdf_path: Path,
    page: int,
    output: Path,
    max_w: int,
    max_h: int,
    *,
    pagebox: str = "mediabox",
) -> bool:
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
        box = pagebox.lower()
        if box == "trimbox" and pg.trimbox:
            clip = pg.trimbox
        elif box == "bleedbox" and pg.bleedbox:
            clip = pg.bleedbox
        else:
            clip = pg.mediabox
        if clip.width <= 0 or clip.height <= 0:
            doc.close()
            return False
        scale = min(max_w / clip.width, max_h / clip.height)
        pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
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
