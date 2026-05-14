import logging
import tempfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from app.processor import adjust_bleed_pdf

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="PDF Bleed Adjuster", version="1.0")

MAX_SIZE = 50 * 1024 * 1024  # 50 MB


def _unlink_safe(path: str | None) -> None:
    if path:
        Path(path).unlink(missing_ok=True)


@app.post("/adjust-bleed")
async def adjust_bleed(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    bleed_mm: float = Form(3.0),
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
        adjust_bleed_pdf(tmp_in, tmp_out, bleed_mm)
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
    return {"status": "ok", "service": "pdf-bleed-adjuster"}