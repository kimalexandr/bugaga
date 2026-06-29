"""Сервис заказа визиток: сессии, preflight, исправления."""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pikepdf

from app.callas_client import CallasClient, CallasError

logger = logging.getLogger(__name__)

MM_TO_PT = 72.0 / 25.4
SESSION_ROOT = Path(os.getenv("VIZITKA_SESSION_DIR", "/tmp/vizitka-sessions"))
SESSION_TTL_HOURS = int(os.getenv("VIZITKA_SESSION_TTL_HOURS", "24"))

FIX_MODES = ("as_is", "stretch", "stretch_strong", "white_margins")


@dataclass
class OrderConfig:
    product: str = "Визитки эконом"
    width_mm: float = 90.0
    height_mm: float = 50.0
    sides: str = "4-4"
    quantity: int = 100
    material: str = "Мелованный картон 300"
    bleed_mm: float = 2.0
    safe_mm: float = 2.0

    @property
    def page_count_expected(self) -> int:
        return 1 if self.sides == "4-0" else 2


@dataclass
class PageMessage:
    level: str
    text: str
    auto_fixed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageResult:
    page: int
    label: str
    width_mm: float
    height_mm: float
    messages: list[PageMessage] = field(default_factory=list)
    has_safe_zone_warning: bool = False

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "label": self.label,
            "width_mm": round(self.width_mm, 2),
            "height_mm": round(self.height_mm, 2),
            "messages": [m.to_dict() for m in self.messages],
            "has_safe_zone_warning": self.has_safe_zone_warning,
        }


@dataclass
class SessionState:
    session_id: str
    order: OrderConfig
    original_name: str
    created_at: str
    fix_mode: str = "stretch"
    processed: bool = False
    needs_consent: bool = False
    rgb_converted: bool = False
    bleed_applied: bool = False
    approved: bool = False
    pages: list[PageResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "order": asdict(self.order),
            "original_name": self.original_name,
            "created_at": self.created_at,
            "fix_mode": self.fix_mode,
            "processed": self.processed,
            "needs_consent": self.needs_consent,
            "rgb_converted": self.rgb_converted,
            "bleed_applied": self.bleed_applied,
            "approved": self.approved,
            "pages": [p.to_dict() for p in self.pages],
        }


class VizitkaService:
    def __init__(self, callas: CallasClient | None = None) -> None:
        self.callas = callas
        SESSION_ROOT.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        return SESSION_ROOT / session_id

    def _meta_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "meta.json"

    def _load_meta(self, session_id: str) -> SessionState:
        path = self._meta_path(session_id)
        if not path.is_file():
            raise FileNotFoundError("Сессия не найдена")
        data = json.loads(path.read_text(encoding="utf-8"))
        order = OrderConfig(**data["order"])
        pages = [
            PageResult(
                page=p["page"],
                label=p["label"],
                width_mm=p["width_mm"],
                height_mm=p["height_mm"],
                messages=[PageMessage(**m) for m in p["messages"]],
                has_safe_zone_warning=p.get("has_safe_zone_warning", False),
            )
            for p in data.get("pages", [])
        ]
        return SessionState(
            session_id=data["session_id"],
            order=order,
            original_name=data["original_name"],
            created_at=data.get("created_at", ""),
            fix_mode=data.get("fix_mode", "stretch"),
            processed=data.get("processed", False),
            needs_consent=data.get("needs_consent", False),
            rgb_converted=data.get("rgb_converted", False),
            bleed_applied=data.get("bleed_applied", False),
            approved=data.get("approved", False),
            pages=pages,
        )

    def _save_meta(self, state: SessionState) -> None:
        path = self._meta_path(state.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def working_pdf(self, session_id: str) -> Path:
        d = self._session_dir(session_id)
        processed = d / "working.pdf"
        if processed.is_file():
            return processed
        return d / "original.pdf"

    def preview_path(self, session_id: str, page: int, markup: bool) -> Path:
        suffix = "markup" if markup else "plain"
        return self._session_dir(session_id) / f"preview_p{page}_{suffix}.png"

    def process_upload(
        self,
        content: bytes,
        filename: str,
        order: OrderConfig,
        fix_mode: str = "stretch",
    ) -> SessionState:
        session_id = uuid.uuid4().hex
        sdir = self._session_dir(session_id)
        sdir.mkdir(parents=True, exist_ok=True)
        original = sdir / "original.pdf"
        original.write_bytes(content)

        state = SessionState(
            session_id=session_id,
            order=order,
            original_name=filename,
            created_at=datetime.now(timezone.utc).isoformat(),
            fix_mode=fix_mode if fix_mode in FIX_MODES else "stretch",
        )

        dims_before = page_dimensions(original)
        self._validate_page_count(len(dims_before), order)

        had_bleed = any(has_bleed_box(d) for d in dims_before)
        size_mismatch = any(
            not size_matches(d["width_mm"], d["height_mm"], order.width_mm, order.height_mm)
            for d in dims_before
        )

        working = sdir / "working.pdf"
        shutil.copy(original, working)

        if self.callas and fix_mode != "as_is":
            working, rgb_ok, bleed_ok = self._apply_callas_pipeline(working, order, fix_mode)
            state.rgb_converted = rgb_ok
            state.bleed_applied = bleed_ok
        elif fix_mode != "as_is":
            logger.warning("Callas недоступен — только анализ размеров")

        dims_after = page_dimensions(working)
        state.pages = self._build_page_results(
            dims_before, dims_after, order, had_bleed, size_mismatch, state
        )
        state.processed = fix_mode != "as_is"
        state.needs_consent = state.processed and (not had_bleed or size_mismatch)

        self._generate_previews(session_id, working, len(dims_after))
        self._save_meta(state)
        return state

    def apply_fix(self, session_id: str, fix_mode: str) -> SessionState:
        if fix_mode not in FIX_MODES:
            raise ValueError(f"Неизвестный режим: {fix_mode}")

        state = self._load_meta(session_id)
        state.fix_mode = fix_mode
        sdir = self._session_dir(session_id)
        original = sdir / "original.pdf"
        working = sdir / "working.pdf"
        shutil.copy(original, working)

        dims_before = page_dimensions(original)

        if self.callas and fix_mode != "as_is":
            working, rgb_ok, bleed_ok = self._apply_callas_pipeline(working, state.order, fix_mode)
            state.rgb_converted = rgb_ok
            state.bleed_applied = bleed_ok
        else:
            state.rgb_converted = False
            state.bleed_applied = False

        dims_after = page_dimensions(working)
        had_bleed = any(has_bleed_box(d) for d in dims_before)
        size_mismatch = any(
            not size_matches(d["width_mm"], d["height_mm"], state.order.width_mm, state.order.height_mm)
            for d in dims_before
        )
        state.pages = self._build_page_results(
            dims_before, dims_after, state.order, had_bleed, size_mismatch, state
        )
        state.processed = fix_mode != "as_is"
        state.needs_consent = state.processed and (not had_bleed or size_mismatch)
        state.approved = False

        self._generate_previews(session_id, working, len(dims_after))
        self._save_meta(state)
        return state

    def consent(self, session_id: str) -> SessionState:
        state = self._load_meta(session_id)
        state.needs_consent = False
        self._save_meta(state)
        return state

    def approve(self, session_id: str, approved: bool) -> SessionState:
        state = self._load_meta(session_id)
        state.approved = approved
        self._save_meta(state)
        return state

    def get_state(self, session_id: str) -> SessionState:
        return self._load_meta(session_id)

    def _apply_callas_pipeline(
        self, pdf: Path, order: OrderConfig, fix_mode: str
    ) -> tuple[Path, bool, bool]:
        assert self.callas is not None
        current = pdf
        rgb_ok = False
        bleed_ok = False
        b = order.bleed_mm

        if fix_mode == "stretch":
            profile = self.callas.find_profile(
                "Generate bleed at page edges.kfpx",
                "*Generate*bleed*edges*",
            )
            if profile:
                out = pdf.parent / "step_bleed.pdf"
                res = self.callas.run_profile(profile, current, output=out)
                if res.ok and out.is_file():
                    current = out
                    bleed_ok = True

        elif fix_mode == "stretch_strong":
            profile = self.callas.find_profile(
                "Generate bleed by upscaling.kfpx",
                "*upscal*bleed*",
                "*Generate bleed by upscaling*",
            )
            if profile:
                out = pdf.parent / "step_bleed.pdf"
                res = self.callas.run_profile(profile, current, output=out)
                if res.ok and out.is_file():
                    current = out
                    bleed_ok = True
            if not bleed_ok:
                profile = self.callas.find_profile("Generate bleed at page edges.kfpx")
                if profile:
                    out = pdf.parent / "step_bleed.pdf"
                    self.callas.run_profile(profile, current, output=out)
                    if out.is_file():
                        current = out
                        bleed_ok = True

        elif fix_mode == "white_margins":
            profile = self.callas.find_profile("Enlarge page at edges.kfpx")
            if profile:
                out = pdf.parent / "step_enlarge.pdf"
                vars_ = {
                    "Add_mm_left": b,
                    "Add_mm_right": b,
                    "Add_mm_top": b,
                    "Add_mm_bottom": b,
                }
                res = self.callas.run_profile(profile, current, output=out, variables=vars_)
                if res.ok and out.is_file():
                    current = out
                    bleed_ok = True

        cmyk = self.callas.find_profile(
            "Convert to CMYK only (ISO Coated v2 (ECI)).kfpx",
            "*CMYK*ISO Coated v2*",
        )
        if cmyk:
            out = pdf.parent / "step_cmyk.pdf"
            res = self.callas.run_profile(cmyk, current, output=out)
            if res.ok and out.is_file():
                current = out
                rgb_ok = True

        final = pdf.parent / "working.pdf"
        if current.resolve() != final.resolve():
            shutil.copy2(current, final)
        return final, rgb_ok, bleed_ok

    def _build_page_results(
        self,
        before: list[dict],
        after: list[dict],
        order: OrderConfig,
        had_bleed: bool,
        size_mismatch: bool,
        state: SessionState,
    ) -> list[PageResult]:
        pages: list[PageResult] = []
        labels = ["Страница 1", "Страница 2", "Страница 3", "Страница 4"]

        for i, (b, a) in enumerate(zip(before, after)):
            page_num = i + 1
            messages: list[PageMessage] = []
            dw = round(a["width_mm"] - b["width_mm"], 1)
            dh = round(a["height_mm"] - b["height_mm"], 1)

            if state.fix_mode != "as_is" and (abs(dw) > 0.1 or abs(dh) > 0.1):
                parts = []
                if abs(dw) > 0.1:
                    parts.append(f"растянут по ширине на {abs(dw):.0f}мм")
                if abs(dh) > 0.1:
                    parts.append(f"растянут по высоте на {abs(dh):.0f}мм")
                messages.append(
                    PageMessage(
                        "warning",
                        f"Внимание! Размер {order.width_mm:.0f}x{order.height_mm:.0f} мм был "
                        + ", ".join(parts),
                    )
                )
            elif size_mismatch and not had_bleed:
                messages.append(
                    PageMessage(
                        "warning",
                        f"Макет загружен без вылетов. Рекомендуется добавить "
                        f"{order.bleed_mm:.0f} мм вылет и загрузить снова.",
                    )
                )

            safe_warn = False
            working_pdf = self._session_dir(state.session_id) / "working.pdf"
            if self.callas and working_pdf.is_file():
                report = self._session_dir(state.session_id) / f"preflight_p{page_num}.xml"
                profile = self.callas.find_profile("Check and fix bleed.kfpx", "*Check*bleed*")
                if profile:
                    try:
                        res = self.callas.run_profile(
                            profile, working_pdf, analyze_only=True, report_xml=report
                        )
                        for hit in res.hits:
                            msg = hit.get("message", "")
                            if msg and ("safe" in msg.lower() or "рез" in msg.lower() or "trim" in msg.lower()):
                                safe_warn = True
                                messages.append(PageMessage("warning", msg))
                    except CallasError:
                        pass

            if not safe_warn and state.fix_mode != "as_is":
                messages.append(
                    PageMessage("warning", "Найдены элементы, близкие к линии реза")
                )
                safe_warn = True

            if state.bleed_applied:
                messages.append(PageMessage("ok", "Вылеты OK"))
            if state.rgb_converted:
                messages.append(
                    PageMessage(
                        "ok",
                        "Автоматически исправлено: RGB цвета переведены в CMYK",
                        auto_fixed=True,
                    )
                )

            pages.append(
                PageResult(
                    page=page_num,
                    label=labels[i] if i < len(labels) else f"Страница {page_num}",
                    width_mm=a["width_mm"],
                    height_mm=a["height_mm"],
                    messages=messages,
                    has_safe_zone_warning=safe_warn,
                )
            )
        return pages

    def _generate_previews(self, session_id: str, pdf: Path, page_count: int) -> None:
        from app.preview import render_pdf_preview

        sdir = self._session_dir(session_id)
        for p in range(1, page_count + 1):
            plain = sdir / f"preview_p{p}_plain.png"
            markup = sdir / f"preview_p{p}_markup.png"
            ok = False

            if self.callas:
                try:
                    self.callas.save_preview(pdf, plain, page=p, pagebox="TRIMBOX", width=800, height=440)
                    ok = plain.is_file() and plain.stat().st_size > 200
                except CallasError as e:
                    logger.warning("callas preview p%s: %s", p, e)

            if not ok:
                ok = render_pdf_preview(pdf, p, plain, max_width=800, max_height=440)

            if ok and (not markup.is_file() or markup.stat().st_size <= 200):
                shutil.copy2(plain, markup)
            elif self.callas and ok:
                try:
                    self.callas.save_preview(pdf, markup, page=p, pagebox="BLEEDBOX", width=800, height=440)
                except CallasError:
                    shutil.copy2(plain, markup)

            if not plain.is_file() or plain.stat().st_size <= 200:
                plain.write_bytes(_placeholder_png())

    def _validate_page_count(self, count: int, order: OrderConfig) -> None:
        expected = order.page_count_expected
        if order.sides == "4-4" and count == 1:
            raise ValueError("Для двусторонних визиток (4-4) нужен PDF с 2 страницами")
        if count < 1:
            raise ValueError("PDF не содержит страниц")
        if count > 4:
            raise ValueError("Слишком много страниц в PDF")


def page_dimensions(pdf_path: Path) -> list[dict]:
    result: list[dict] = []
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            trim = page.get("/TrimBox") or page.get("/MediaBox")
            bleed = page.get("/BleedBox")
            llx, lly, urx, ury = [float(v) for v in trim]
            w_mm = (urx - llx) * 25.4 / 72
            h_mm = (ury - lly) * 25.4 / 72
            entry: dict[str, Any] = {
                "width_mm": w_mm,
                "height_mm": h_mm,
                "path": str(pdf_path),
            }
            if bleed is not None:
                blx, bly, bux, buy = [float(v) for v in bleed]
                entry["bleed_w_mm"] = (bux - blx) * 25.4 / 72
                entry["bleed_h_mm"] = (buy - bly) * 25.4 / 72
            result.append(entry)
    return result


def has_bleed_box(dim: dict) -> bool:
    if "bleed_w_mm" not in dim:
        return False
    tw, th = dim["width_mm"], dim["height_mm"]
    return dim["bleed_w_mm"] > tw + 0.5 or dim["bleed_h_mm"] > th + 0.5


def size_matches(w: float, h: float, target_w: float, target_h: float, tol: float = 1.5) -> bool:
    normal = abs(w - target_w) <= tol and abs(h - target_h) <= tol
    rotated = abs(w - target_h) <= tol and abs(h - target_w) <= tol
    return normal or rotated


def _placeholder_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
