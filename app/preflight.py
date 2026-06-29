"""Реальный preflight PDF: размер, вылеты, RGB."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pikepdf

MM_TO_PT = 72.0 / 25.4


@dataclass
class PagePreflight:
    page: int
    trim_w_mm: float
    trim_h_mm: float
    media_w_mm: float
    media_h_mm: float
    bleed_w_mm: float | None = None
    bleed_h_mm: float | None = None
    bleed_margin_mm: float | None = None
    has_bleed: bool = False
    size_ok: bool = False
    has_rgb: bool = False
    messages: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "trim_w_mm": round(self.trim_w_mm, 2),
            "trim_h_mm": round(self.trim_h_mm, 2),
            "media_w_mm": round(self.media_w_mm, 2),
            "media_h_mm": round(self.media_h_mm, 2),
            "bleed_margin_mm": round(self.bleed_margin_mm, 2) if self.bleed_margin_mm is not None else None,
            "has_bleed": self.has_bleed,
            "size_ok": self.size_ok,
            "has_rgb": self.has_rgb,
            "messages": self.messages,
        }


def _pt_to_mm(pt: float) -> float:
    return pt * 25.4 / 72.0


def _box_mm(box) -> tuple[float, float]:
    llx, lly, urx, ury = [float(v) for v in box]
    return _pt_to_mm(urx - llx), _pt_to_mm(ury - lly)


def size_matches(w: float, h: float, tw: float, th: float, tol: float = 1.5) -> bool:
    return (abs(w - tw) <= tol and abs(h - th) <= tol) or (
        abs(w - th) <= tol and abs(h - tw) <= tol
    )


def _page_has_rgb(page) -> bool:
    found = False

    def walk(obj):
        nonlocal found
        if found:
            return
        if isinstance(obj, pikepdf.Array):
            for item in obj:
                walk(item)
            return
        if not isinstance(obj, pikepdf.Object):
            return
        try:
            if obj.get("/ColorSpace") is not None:
                cs = str(obj["/ColorSpace"])
                if "/DeviceRGB" in cs or "RGB" in cs.upper():
                    found = True
                    return
            if obj.get("/CS") is not None:
                cs = str(obj["/CS"])
                if "RGB" in cs.upper():
                    found = True
                    return
        except (AttributeError, TypeError, KeyError):
            pass
        if isinstance(obj, pikepdf.Dictionary):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, pikepdf.Stream):
            try:
                cs = obj.get("/ColorSpace")
                if cs is not None and "RGB" in str(cs).upper():
                    found = True
            except (AttributeError, TypeError):
                pass

    try:
        res = page.get("/Resources")
        if res:
            walk(res)
        contents = page.get("/Contents")
        if contents:
            walk(contents)
    except Exception:
        pass
    return found


def analyze_pdf(
    pdf_path: Path,
    *,
    target_w_mm: float,
    target_h_mm: float,
    required_bleed_mm: float = 2.0,
) -> list[PagePreflight]:
    results: list[PagePreflight] = []
    with pikepdf.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            media = page.get("/MediaBox")
            trim = page.get("/TrimBox") or media
            bleed = page.get("/BleedBox")

            trim_w, trim_h = _box_mm(trim)
            media_w, media_h = _box_mm(media)

            pf = PagePreflight(
                page=i + 1,
                trim_w_mm=trim_w,
                trim_h_mm=trim_h,
                media_w_mm=media_w,
                media_h_mm=media_h,
                size_ok=size_matches(trim_w, trim_h, target_w_mm, target_h_mm),
                has_rgb=_page_has_rgb(page),
            )

            bleed_margin = None
            if bleed is not None:
                bw, bh = _box_mm(bleed)
                pf.bleed_w_mm = bw
                pf.bleed_h_mm = bh
                margin_w = max(0.0, (bw - trim_w) / 2)
                margin_h = max(0.0, (bh - trim_h) / 2)
                bleed_margin = min(margin_w, margin_h) if margin_w and margin_h else max(margin_w, margin_h)
            else:
                margin_w = max(0.0, (media_w - trim_w) / 2)
                margin_h = max(0.0, (media_h - trim_h) / 2)
                bleed_margin = min(margin_w, margin_h) if margin_w and margin_h else max(margin_w, margin_h)

            pf.bleed_margin_mm = bleed_margin
            pf.has_bleed = bleed_margin is not None and bleed_margin >= required_bleed_mm - 0.3

            if not pf.size_ok:
                pf.messages.append(
                    {
                        "level": "warning",
                        "text": (
                            f"Размер линии реза {trim_w:.1f}×{trim_h:.1f} мм, "
                            f"ожидается {target_w_mm:.0f}×{target_h_mm:.0f} мм."
                        ),
                    }
                )

            if not pf.has_bleed:
                pf.messages.append(
                    {
                        "level": "warning",
                        "text": (
                            f"Макет без вылетов (запас от реза ~{bleed_margin or 0:.1f} мм, "
                            f"нужно ≥{required_bleed_mm:.0f} мм)."
                        ),
                    }
                )
            else:
                pf.messages.append(
                    {
                        "level": "ok",
                        "text": f"Вылеты в PDF: ~{bleed_margin:.1f} мм от линии реза.",
                    }
                )

            if pf.has_rgb:
                pf.messages.append(
                    {
                        "level": "warning",
                        "text": "В макете есть RGB-объекты (для печати нужен CMYK).",
                    }
                )

            results.append(pf)
    return results
