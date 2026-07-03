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
from app.preflight import PagePreflight, analyze_pdf
from app.processor import adjust_bleed_pdf, apply_mirror_bleed_pdf, apply_white_margins_pdf

logger = logging.getLogger(__name__)

MM_TO_PT = 72.0 / 25.4
SESSION_ROOT = Path(os.getenv("VIZITKA_SESSION_DIR", "/tmp/vizitka-sessions"))
SESSION_TTL_HOURS = int(os.getenv("VIZITKA_SESSION_TTL_HOURS", "24"))
TRIM_PREVIEW_OFFSET_MM = float(os.getenv("TRIM_PREVIEW_OFFSET_MM", "1.0"))
DEFAULT_FIX_MODE = os.getenv("DEFAULT_FIX_MODE", "mirror_bleed")

FIX_MODES = ("as_is", "stretch", "stretch_strong", "white_margins", "mirror_bleed")


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
    trim_preview_offset_mm: float = TRIM_PREVIEW_OFFSET_MM

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
    needs_fix: bool = False
    preflight: dict | None = None

    def to_dict(self) -> dict:
        d = {
            "page": self.page,
            "label": self.label,
            "width_mm": round(self.width_mm, 2),
            "height_mm": round(self.height_mm, 2),
            "messages": [m.to_dict() for m in self.messages],
            "has_safe_zone_warning": self.has_safe_zone_warning,
            "needs_fix": self.needs_fix,
        }
        if self.preflight:
            d["preflight"] = self.preflight
        return d


@dataclass
class SessionState:
    session_id: str
    order: OrderConfig
    original_name: str
    created_at: str
    fix_mode: str = DEFAULT_FIX_MODE
    processed: bool = False
    needs_consent: bool = False
    rgb_converted: bool = False
    bleed_applied: bool = False
    cmyk_pending: bool = False
    approved: bool = False
    preview_ready: bool = False
    processing: bool = False
    processing_error: str | None = None
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
            "cmyk_pending": self.cmyk_pending,
            "approved": self.approved,
            "preview_ready": self.preview_ready,
            "processing": self.processing,
            "processing_error": self.processing_error,
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
        order_data = dict(data["order"])
        order_data.setdefault("trim_preview_offset_mm", TRIM_PREVIEW_OFFSET_MM)
        order = OrderConfig(**order_data)
        pages = [
            PageResult(
                page=p["page"],
                label=p["label"],
                width_mm=p["width_mm"],
                height_mm=p["height_mm"],
                messages=[PageMessage(**m) for m in p["messages"]],
                has_safe_zone_warning=p.get("has_safe_zone_warning", False),
                needs_fix=p.get("needs_fix", False),
                preflight=p.get("preflight"),
            )
            for p in data.get("pages", [])
        ]
        return SessionState(
            session_id=data["session_id"],
            order=order,
            original_name=data["original_name"],
            created_at=data.get("created_at", ""),
            fix_mode=data.get("fix_mode", DEFAULT_FIX_MODE),
            processed=data.get("processed", False),
            needs_consent=data.get("needs_consent", False),
            rgb_converted=data.get("rgb_converted", False),
            bleed_applied=data.get("bleed_applied", False),
            cmyk_pending=data.get("cmyk_pending", False),
            approved=data.get("approved", False),
            preview_ready=data.get("preview_ready", False),
            processing=data.get("processing", False),
            processing_error=data.get("processing_error"),
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

    def begin_upload(
        self,
        content: bytes,
        filename: str,
        order: OrderConfig,
        fix_mode: str = DEFAULT_FIX_MODE,
    ) -> SessionState:
        """Быстрое сохранение PDF и валидация — ответ до таймаута nginx."""
        session_id = uuid.uuid4().hex
        sdir = self._session_dir(session_id)
        sdir.mkdir(parents=True, exist_ok=True)
        original = sdir / "original.pdf"
        original.write_bytes(content)
        shutil.copy(original, sdir / "working.pdf")

        state = SessionState(
            session_id=session_id,
            order=order,
            original_name=filename,
            created_at=datetime.now(timezone.utc).isoformat(),
            fix_mode=fix_mode if fix_mode in FIX_MODES else DEFAULT_FIX_MODE,
            processing=True,
        )

        page_count = len(analyze_pdf(original, target_w_mm=order.width_mm, target_h_mm=order.height_mm))
        self._validate_page_count(page_count, order)

        dims_before = analyze_pdf(
            original,
            target_w_mm=order.width_mm,
            target_h_mm=order.height_mm,
            required_bleed_mm=order.bleed_mm,
        )
        state.pages = self._placeholder_pages(dims_before, order)
        self._save_meta(state)
        return state

    def finish_upload(self, session_id: str) -> None:
        """Тяжёлая обработка (Callas CMYK, вылеты, превью) — в фоне."""
        state = self._load_meta(session_id)
        try:
            sdir = self._session_dir(session_id)
            original = sdir / "original.pdf"
            working = sdir / "working.pdf"
            shutil.copy(original, working)

            dims_before_pf = analyze_pdf(
                original,
                target_w_mm=state.order.width_mm,
                target_h_mm=state.order.height_mm,
                required_bleed_mm=state.order.bleed_mm,
            )

            if state.fix_mode != "as_is":
                working, rgb_ok, bleed_ok = self._apply_fix_pipeline(
                    working, state.order, state.fix_mode, convert_cmyk=True
                )
                state.rgb_converted = rgb_ok
                state.bleed_applied = bleed_ok
                state.cmyk_pending = False
            else:
                state.rgb_converted = False
                state.bleed_applied = False
                state.cmyk_pending = False

            dims_after_pf = analyze_pdf(
                working,
                target_w_mm=state.order.width_mm,
                target_h_mm=state.order.height_mm,
                required_bleed_mm=state.order.bleed_mm,
            )
            state.pages = self._build_page_results(dims_before_pf, dims_after_pf, state.order, state)
            state.processed = state.fix_mode != "as_is"
            state.needs_consent = state.processed and any(
                not a.has_bleed
                or not a.size_ok
                or (a.has_rgb and not state.rgb_converted and not state.cmyk_pending)
                for a in dims_after_pf
            )
            state.preview_ready = self._generate_previews(
                session_id, working, len(dims_after_pf), state.order
            )
            state.processing = False
            state.processing_error = None
            logger.info("finish_upload ok: %s", session_id)
        except Exception as e:
            logger.exception("finish_upload %s", session_id)
            state.processing = False
            state.processing_error = str(e)
        self._save_meta(state)

    def process_upload(
        self,
        content: bytes,
        filename: str,
        order: OrderConfig,
        fix_mode: str = DEFAULT_FIX_MODE,
    ) -> SessionState:
        state = self.begin_upload(content, filename, order, fix_mode=fix_mode)
        self.finish_upload(state.session_id)
        return self._load_meta(state.session_id)

    def _placeholder_pages(self, dims: list[PagePreflight], _order: OrderConfig) -> list[PageResult]:
        labels = ["Страница 1", "Страница 2", "Страница 3", "Страница 4"]
        pages: list[PageResult] = []
        for i, a in enumerate(dims):
            messages = [
                PageMessage("info", "Обработка макета (вылеты, CMYK)…"),
                *[PageMessage(m["level"], m["text"]) for m in a.messages],
            ]
            pages.append(
                PageResult(
                    page=i + 1,
                    label=labels[i] if i < len(labels) else f"Страница {i + 1}",
                    width_mm=a.trim_w_mm,
                    height_mm=a.trim_h_mm,
                    messages=messages,
                    preflight=a.to_dict(),
                )
            )
        return pages

    def apply_fix(
        self, session_id: str, fix_mode: str, *, convert_cmyk: bool = True
    ) -> SessionState:
        if fix_mode not in FIX_MODES:
            raise ValueError(f"Неизвестный режим: {fix_mode}")

        state = self._load_meta(session_id)
        state.fix_mode = fix_mode
        sdir = self._session_dir(session_id)
        original = sdir / "original.pdf"
        working = sdir / "working.pdf"
        shutil.copy(original, working)

        dims_before_pf = analyze_pdf(
            original,
            target_w_mm=state.order.width_mm,
            target_h_mm=state.order.height_mm,
            required_bleed_mm=state.order.bleed_mm,
        )

        if fix_mode != "as_is":
            working, rgb_ok, bleed_ok = self._apply_fix_pipeline(
                working, state.order, fix_mode, convert_cmyk=convert_cmyk
            )
            state.rgb_converted = rgb_ok
            state.bleed_applied = bleed_ok
            state.cmyk_pending = fix_mode != "as_is" and not convert_cmyk
        else:
            state.rgb_converted = False
            state.bleed_applied = False
            state.cmyk_pending = False

        dims_after_pf = analyze_pdf(
            working,
            target_w_mm=state.order.width_mm,
            target_h_mm=state.order.height_mm,
            required_bleed_mm=state.order.bleed_mm,
        )
        state.pages = self._build_page_results(dims_before_pf, dims_after_pf, state.order, state)
        state.processed = fix_mode != "as_is"
        state.needs_consent = state.processed and any(
            not a.has_bleed
            or not a.size_ok
            or (a.has_rgb and not state.rgb_converted and not state.cmyk_pending)
            for a in dims_after_pf
        )
        state.approved = False
        state.preview_ready = self._generate_previews(
            session_id, working, len(dims_after_pf), state.order
        )
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

    def update_preview_settings(
        self,
        session_id: str,
        *,
        trim_preview_offset_mm: float | None = None,
        safe_mm: float | None = None,
    ) -> SessionState:
        state = self._load_meta(session_id)
        if trim_preview_offset_mm is not None:
            state.order.trim_preview_offset_mm = max(0.0, min(3.0, float(trim_preview_offset_mm)))
        if safe_mm is not None:
            state.order.safe_mm = max(0.0, min(10.0, float(safe_mm)))
        pdf = self.working_pdf(session_id)
        count = len(
            analyze_pdf(pdf, target_w_mm=state.order.width_mm, target_h_mm=state.order.height_mm)
        )
        state.preview_ready = self._generate_previews(session_id, pdf, count, state.order)
        self._save_meta(state)
        return state

    def get_state(self, session_id: str) -> SessionState:
        return self._load_meta(session_id)

    def _apply_fix_pipeline(
        self,
        pdf: Path,
        order: OrderConfig,
        fix_mode: str,
        *,
        convert_cmyk: bool = True,
    ) -> tuple[Path, bool, bool]:
        rgb_ok = False
        bleed_ok = False

        if self.callas:
            pdf, rgb_ok, bleed_ok = self._apply_callas_pipeline(
                pdf, order, fix_mode, convert_cmyk=convert_cmyk
            )

        if fix_mode == "mirror_bleed":
            tmp = pdf.parent / "step_mirror.pdf"
            try:
                apply_mirror_bleed_pdf(str(pdf), str(tmp), order.bleed_mm)
                if tmp.is_file():
                    shutil.copy2(tmp, pdf)
                    bleed_ok = True
                    logger.info("зеркальные вылеты (%.1f мм)", order.bleed_mm)
            except Exception as e:
                logger.warning("mirror bleed: %s", e)

        elif fix_mode == "white_margins" and not bleed_ok:
            tmp = pdf.parent / "step_white.pdf"
            try:
                apply_white_margins_pdf(str(pdf), str(tmp), margin_mm=order.bleed_mm)
                if tmp.is_file():
                    shutil.copy2(tmp, pdf)
                    bleed_ok = True
                    logger.info("белые поля внутри TrimBox (%.1f мм)", order.bleed_mm)
            except Exception as e:
                logger.warning("white margins: %s", e)

        if not bleed_ok and fix_mode in ("stretch", "stretch_strong", "white_margins"):
            tmp = pdf.parent / "step_boxes.pdf"
            bleed_amount = order.bleed_mm
            if fix_mode == "stretch_strong":
                bleed_amount = order.bleed_mm * 2
            try:
                adjust_bleed_pdf(str(pdf), str(tmp), bleed_amount, add_crop_marks=False)
                if tmp.is_file():
                    shutil.copy2(tmp, pdf)
                    bleed_ok = True
                    logger.info(
                        "вылеты через pikepdf (%.1f мм, режим %s)",
                        bleed_amount,
                        fix_mode,
                    )
            except Exception as e:
                logger.warning("pikepdf bleed: %s", e)

        return pdf, rgb_ok, bleed_ok

    def _apply_callas_pipeline(
        self,
        pdf: Path,
        order: OrderConfig,
        fix_mode: str,
        *,
        convert_cmyk: bool = True,
    ) -> tuple[Path, bool, bool]:
        assert self.callas is not None
        current = pdf
        rgb_ok = False
        bleed_ok = False
        b = order.bleed_mm
        bleed_vars = {
            "Bleed_mm": b,
            "bleed_mm": b,
            "Add_mm_left": b,
            "Add_mm_right": b,
            "Add_mm_top": b,
            "Add_mm_bottom": b,
        }

        if fix_mode == "stretch":
            profile = self.callas.find_profile(
                "Generate bleed at page edges.kfpx",
                "*Generate*bleed*edges*",
            )
            if profile:
                out = pdf.parent / "step_bleed.pdf"
                res = self.callas.run_profile(
                    profile, current, output=out, variables=bleed_vars
                )
                if res.ok and out.is_file():
                    current = out
                    bleed_ok = True
                else:
                    logger.warning(
                        "bleed профиль %s: exit=%s out=%s",
                        profile.name,
                        res.returncode,
                        (res.stderr or res.stdout or "")[:500],
                    )
            else:
                logger.warning("профиль bleed edges не найден в %s/var/Profiles", self.callas.home)

        elif fix_mode == "stretch_strong":
            profile = self.callas.find_profile(
                "Generate bleed by upscaling.kfpx",
                "*upscal*bleed*",
                "*Generate bleed by upscaling*",
            )
            if profile:
                out = pdf.parent / "step_bleed.pdf"
                res = self.callas.run_profile(
                    profile, current, output=out, variables=bleed_vars
                )
                if res.ok and out.is_file():
                    current = out
                    bleed_ok = True
            if not bleed_ok:
                profile = self.callas.find_profile("Generate bleed at page edges.kfpx")
                if profile:
                    out = pdf.parent / "step_bleed.pdf"
                    res = self.callas.run_profile(
                        profile, current, output=out, variables=bleed_vars
                    )
                    if res.ok and out.is_file():
                        current = out
                        bleed_ok = True

        elif fix_mode == "white_margins":
            tmp = pdf.parent / "step_white.pdf"
            try:
                apply_white_margins_pdf(str(current), str(tmp), margin_mm=b)
                if tmp.is_file():
                    current = tmp
                    bleed_ok = True
            except Exception as e:
                logger.warning("white margins: %s", e)

        if convert_cmyk:
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
                else:
                    logger.warning(
                        "CMYK профиль %s: exit=%s stderr=%s",
                        cmyk.name,
                        res.returncode,
                        (res.stderr or res.stdout or "")[:500],
                    )
            else:
                logger.warning("профиль CMYK не найден в %s/var/Profiles", self.callas.home)

        final = pdf.parent / "working.pdf"
        if current.resolve() != final.resolve():
            shutil.copy2(current, final)
        return final, rgb_ok, bleed_ok

    def _build_page_results(
        self,
        before: list[PagePreflight],
        after: list[PagePreflight],
        order: OrderConfig,
        state: SessionState,
    ) -> list[PageResult]:
        pages: list[PageResult] = []
        labels = ["Страница 1", "Страница 2", "Страница 3", "Страница 4"]

        for i, (b, a) in enumerate(zip(before, after)):
            page_num = i + 1
            messages: list[PageMessage] = []

            # Сообщения по состоянию ПОСЛЕ обработки (working.pdf), не по исходнику
            for m in a.messages:
                messages.append(PageMessage(m["level"], m["text"]))

            if state.fix_mode != "as_is":
                dw = round(a.trim_w_mm - b.trim_w_mm, 1)
                dh = round(a.trim_h_mm - b.trim_h_mm, 1)
                if abs(dw) > 0.2 or abs(dh) > 0.2:
                    parts = []
                    if abs(dw) > 0.2:
                        parts.append(f"ширина изменена на {abs(dw):.1f} мм")
                    if abs(dh) > 0.2:
                        parts.append(f"высота изменена на {abs(dh):.1f} мм")
                    messages.append(
                        PageMessage(
                            "warning",
                            f"После обработки: {', '.join(parts)}.",
                        )
                    )

                if a.has_bleed and not b.has_bleed:
                    messages.append(
                        PageMessage(
                            "ok",
                            "Автоматически исправлено: вылеты добавлены",
                            auto_fixed=True,
                        )
                    )

                if state.rgb_converted:
                    messages.append(
                        PageMessage(
                            "ok",
                            "Автоматически исправлено: RGB → CMYK (Callas, ISO Coated v2)",
                            auto_fixed=True,
                        )
                    )
                elif state.cmyk_pending:
                    messages.append(
                        PageMessage(
                            "warning",
                            "Превью: CMYK ещё не применён — нажмите OK для финальной обработки файла.",
                        )
                    )
                elif a.has_rgb and not state.rgb_converted:
                    if self.callas:
                        messages.append(
                            PageMessage(
                                "warning",
                                "RGB остаётся — профиль CMYK Callas не сработал (лицензия, путь или ошибка CLI).",
                            )
                        )
                    else:
                        messages.append(
                            PageMessage(
                                "warning",
                                "RGB остаётся — Callas недоступен в контейнере (см. /health).",
                            )
                        )

            safe_warn = False
            if self.callas:
                working_pdf = self._session_dir(state.session_id) / "working.pdf"
                profile = self.callas.find_profile("Check and fix bleed.kfpx", "*Check*bleed*")
                if profile and working_pdf.is_file():
                    report = self._session_dir(state.session_id) / f"preflight_p{page_num}.xml"
                    try:
                        res = self.callas.run_profile(
                            profile, working_pdf, analyze_only=True, report_xml=report
                        )
                        for hit in res.hits:
                            msg = (hit.get("message") or "").strip()
                            if msg:
                                safe_warn = True
                                messages.append(PageMessage("warning", msg))
                    except CallasError:
                        pass

            needs_fix = not a.has_bleed or not a.size_ok or safe_warn

            pages.append(
                PageResult(
                    page=page_num,
                    label=labels[i] if i < len(labels) else f"Страница {page_num}",
                    width_mm=a.trim_w_mm,
                    height_mm=a.trim_h_mm,
                    messages=messages,
                    has_safe_zone_warning=safe_warn,
                    needs_fix=needs_fix,
                    preflight=a.to_dict(),
                )
            )
        return pages

    def ensure_preview(self, session_id: str, page: int, markup: bool = False) -> Path:
        path = self.preview_path(session_id, page, markup)
        pdf = self.working_pdf(session_id)
        pdf_mtime = pdf.stat().st_mtime if pdf.is_file() else 0
        if path.is_file() and path.stat().st_size > 500 and path.stat().st_mtime >= pdf_mtime:
            return path
        state = self._load_meta(session_id)
        count = len(
            analyze_pdf(pdf, target_w_mm=state.order.width_mm, target_h_mm=state.order.height_mm)
        )
        self._generate_previews(session_id, pdf, count, state.order)
        return path

    def _generate_previews(
        self, session_id: str, pdf: Path, page_count: int, order: OrderConfig
    ) -> bool:
        from app.preview import render_markup_preview, render_pdf_preview

        sdir = self._session_dir(session_id)
        all_ok = True
        for p in range(1, page_count + 1):
            plain = sdir / f"preview_p{p}_plain.png"
            markup = sdir / f"preview_p{p}_markup.png"
            plain.unlink(missing_ok=True)
            markup.unlink(missing_ok=True)
            plain_ok = False
            markup_ok = False
            trim_off = order.trim_preview_offset_mm

            if self.callas:
                try:
                    self.callas.save_preview(
                        pdf, plain, page=p, pagebox="TRIMBOX", width=900, height=500
                    )
                    plain_ok = plain.is_file() and plain.stat().st_size > 500
                except CallasError as e:
                    logger.warning("callas plain preview p%s: %s", p, e)

            if not plain_ok:
                plain_ok = render_pdf_preview(
                    pdf, p, plain, max_width=900, max_height=500, pagebox="trimbox"
                )

            if self.callas and trim_off <= 0:
                try:
                    self.callas.save_safety_preview(
                        pdf,
                        markup,
                        page=p,
                        safe_mm=order.safe_mm,
                        use_bleed=True,
                        width=900,
                        height=500,
                    )
                    markup_ok = markup.is_file() and markup.stat().st_size > 500
                except CallasError as e:
                    logger.warning("callas markup preview p%s: %s", p, e)

            if not markup_ok:
                markup_ok = render_markup_preview(
                    pdf,
                    p,
                    markup,
                    safe_mm=order.safe_mm,
                    trim_offset_mm=trim_off,
                    max_width=900,
                    max_height=500,
                )

            if not plain_ok and markup_ok:
                shutil.copy2(markup, plain)
                plain_ok = True
            if plain_ok and not markup_ok:
                shutil.copy2(plain, markup)

            if not (plain_ok and markup_ok):
                all_ok = False
                logger.error("не удалось создать превью стр.%s для %s", p, pdf)

        return all_ok

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
