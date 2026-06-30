import base64
import logging
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

from app.callas_client import CallasClient, CallasError
from app.processor import adjust_bleed_pdf
from app.vizitka_service import FIX_MODES, OrderConfig, VizitkaService

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="PDF Bleed Adjuster", version="2.0")

_STATIC = Path(__file__).resolve().parent / "static"
MAX_SIZE = 50 * 1024 * 1024

_callas: CallasClient | None = None
_vizitka: VizitkaService | None = None


def _get_callas() -> CallasClient | None:
    global _callas
    if _callas is None:
        try:
            client = CallasClient()
            if client.available():
                _callas = client
            else:
                logging.warning("Callas CLI недоступен")
        except CallasError as e:
            logging.warning("Callas: %s", e)
    return _callas


def _get_vizitka() -> VizitkaService:
    global _vizitka
    if _vizitka is None:
        _vizitka = VizitkaService(callas=_get_callas())
    return _vizitka


def _unlink_safe(path: str | None) -> None:
    if path:
        Path(path).unlink(missing_ok=True)


@app.get("/")
async def vizitka_page():
    page = _STATIC / "vizitka.html"
    if not page.is_file():
        raise HTTPException(404, "vizitka.html не найден")
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/bleed")
async def bleed_page():
    page = _STATIC / "index.html"
    if not page.is_file():
        raise HTTPException(404, "index.html не найден")
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.post("/api/vizitka/upload")
async def vizitka_upload(
    file: UploadFile = File(...),
    width_mm: float = Form(90.0),
    height_mm: float = Form(50.0),
    sides: str = Form("4-4"),
    quantity: int = Form(100),
    material: str = Form("Мелованный картон 300"),
    bleed_mm: float = Form(2.0),
    safe_mm: float = Form(2.0),
    fix_mode: str = Form("stretch"),
):
    if file.content_type != "application/pdf":
        raise HTTPException(400, "Разрешены только PDF")

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(400, "Файл >50MB не поддерживается")

    order = OrderConfig(
        width_mm=width_mm,
        height_mm=height_mm,
        sides=sides,
        quantity=quantity,
        material=material,
        bleed_mm=bleed_mm,
        safe_mm=safe_mm,
    )

    try:
        state = _get_vizitka().process_upload(
            content,
            file.filename or "maket.pdf",
            order,
            fix_mode=fix_mode,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        logging.exception("upload error")
        raise HTTPException(500, f"Ошибка обработки: {e}") from e

    return _enrich_response(state)


@app.post("/api/vizitka/{session_id}/fix")
async def vizitka_fix(session_id: str, fix_mode: str = Form(...), preview_only: int = Form(0)):
    if fix_mode not in FIX_MODES:
        raise HTTPException(400, f"Режим должен быть один из: {', '.join(FIX_MODES)}")

    try:
        state = _get_vizitka().apply_fix(
            session_id, fix_mode, convert_cmyk=not bool(preview_only)
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        logging.exception("fix error")
        raise HTTPException(500, f"Ошибка: {e}") from e

    return _enrich_response(state)


@app.post("/api/vizitka/{session_id}/consent")
async def vizitka_consent(session_id: str):
    try:
        state = _get_vizitka().consent(session_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return _enrich_response(state)


@app.post("/api/vizitka/{session_id}/approve")
async def vizitka_approve(session_id: str, approved: int = Form(1)):
    try:
        state = _get_vizitka().approve(session_id, bool(approved))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return _enrich_response(state)


@app.get("/api/vizitka/{session_id}")
async def vizitka_state(session_id: str):
    try:
        state = _get_vizitka().get_state(session_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return _enrich_response(state)


@app.get("/api/vizitka/{session_id}/preview/{page}")
async def vizitka_preview(session_id: str, page: int, markup: int = 0):
    svc = _get_vizitka()
    try:
        path = svc.ensure_preview(session_id, page, bool(markup))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    if not path.is_file() or path.stat().st_size < 500:
        raise HTTPException(404, "Превью не удалось сгенерировать")
    return FileResponse(path, media_type="image/png")


@app.get("/api/vizitka/{session_id}/download")
async def vizitka_download(session_id: str):
    svc = _get_vizitka()
    try:
        state = svc.get_state(session_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e

    if not state.approved:
        raise HTTPException(400, "Макет не согласован")

    pdf = svc.working_pdf(session_id)
    if not pdf.is_file():
        raise HTTPException(404, "PDF не найден")

    return FileResponse(
        pdf,
        media_type="application/pdf",
        filename=f"vizitka_{session_id[:8]}.pdf",
    )


def _enrich_response(state) -> dict:
    data = state.to_dict()
    sid = state.session_id
    svc = _get_vizitka()
    for p in data["pages"]:
        n = p["page"]
        p["preview_plain"] = f"/api/vizitka/{sid}/preview/{n}?markup=0"
        p["preview_markup"] = f"/api/vizitka/{sid}/preview/{n}?markup=1"
        for key, markup in (("preview_plain_b64", False), ("preview_markup_b64", True)):
            try:
                path = svc.ensure_preview(sid, n, markup)
                if path.is_file() and path.stat().st_size > 500:
                    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
                    p[key] = f"data:image/png;base64,{b64}"
            except (FileNotFoundError, OSError) as e:
                logging.warning("preview b64 p%s: %s", n, e)
    data["callas_available"] = _get_callas() is not None
    data["preview_ready"] = getattr(state, "preview_ready", False)
    working = svc.working_pdf(sid)
    data["processing"] = {
        "uses_real_pdf": working.is_file(),
        "working_pdf": working.name if working.is_file() else None,
        "callas": data["callas_available"],
        "bleed_applied": state.bleed_applied,
        "rgb_converted": state.rgb_converted,
        "cmyk_pending": state.cmyk_pending,
        "fix_mode": state.fix_mode,
    }
    data["can_proceed"] = not state.needs_consent and all(
        not any(m["level"] == "error" for m in p["messages"]) for p in data["pages"]
    )
    return data


@app.post("/adjust-bleed")
async def adjust_bleed(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    bleed_mm: float = Form(3.0),
    add_crop_marks: int = Form(0),
    crop_mark_len_mm: float = Form(3.0),
):
    if file.content_type != "application/pdf":
        raise HTTPException(400, "Разрешены только PDF")

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(400, "Файл >50MB не поддерживается")

    tmp_in = tmp_out = None
    ok = False
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f_in:
            f_in.write(content)
            tmp_in = f_in.name

        tmp_out = tmp_in.replace(".pdf", "_bleed.pdf")
        adjust_bleed_pdf(
            tmp_in,
            tmp_out,
            bleed_mm,
            add_crop_marks=bool(add_crop_marks),
            crop_mark_len_mm=crop_mark_len_mm,
        )
        ok = True
        background_tasks.add_task(_unlink_safe, tmp_out)

        return FileResponse(
            tmp_out,
            media_type="application/pdf",
            filename=f"bleed_{bleed_mm}mm_{file.filename}",
        )
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        raise HTTPException(500, f"Ошибка обработки: {str(e)}")
    finally:
        _unlink_safe(tmp_in)
        if not ok:
            _unlink_safe(tmp_out)


@app.get("/health")
def health():
    callas = _get_callas()
    profiles: dict[str, str | None] = {}
    if callas:
        try:
            profiles = callas.list_key_profiles()
        except CallasError:
            profiles = {}
    callas_home = os.getenv("CALLAS_HOME", "/opt/pdftoolbox/callas_pdfToolboxCLI_x64_Linux_17-0-682")
    lang_home = Path(callas_home)
    data: dict = {
        "status": "ok",
        "service": "pdf-bleed-adjuster",
        "callas": callas is not None,
        "callas_home": callas_home,
        "callas_language": os.getenv("CALLAS_LANGUAGE")
        or ("ru" if (lang_home / "lang" / "pdfToolbox.ru.bin").is_file() else None)
        or ("en" if (lang_home / "lang" / "pdfToolbox.en.bin").is_file() else "default"),
        "callas_cache_folder": os.getenv("CALLAS_CACHE_FOLDER") or None,
        "profiles": profiles,
        "profiles_ok": bool(profiles.get("bleed_edges") and profiles.get("cmyk")),
    }
    if callas and os.getenv("CALLAS_LICENSE_PROBE", "").lower() in ("1", "true", "yes"):
        probe_pdf = Path(os.getenv("CALLAS_LICENSE_PROBE_PDF", ""))
        if not probe_pdf.is_file():
            sessions = Path(os.getenv("VIZITKA_SESSION_DIR", "/tmp/vizitka-sessions"))
            if sessions.is_dir():
                for sdir in sorted(sessions.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                    candidate = sdir / "working.pdf"
                    if candidate.is_file():
                        probe_pdf = candidate
                        break
        try:
            lic = callas.license_probe(probe_pdf if probe_pdf.is_file() else None)
            data["callas_license"] = lic
        except CallasError as e:
            data["callas_license"] = {"licensed": False, "detail": str(e)}
    return data
