import logging

import pikepdf
from pikepdf import Name

MM_TO_PT = 72.0 / 25.4


def _crop_mark_ops(llx: float, lly: float, urx: float, ury: float, mark_pt: float) -> bytes:
    """Угловые засечки снаружи TrimBox: по две линии от каждого угла (PDF user space, pt)."""
    m = mark_pt

    def F(x: float) -> str:
        return f"{x:.4f}"

    ops = [
        "q",
        "0.35 w",
        "0 0 0 RG",
        # нижний левый
        f"{F(llx - m)} {F(lly)} m {F(llx)} {F(lly)} l S",
        f"{F(llx)} {F(lly - m)} m {F(llx)} {F(lly)} l S",
        # нижний правый
        f"{F(urx)} {F(lly)} m {F(urx + m)} {F(lly)} l S",
        f"{F(urx)} {F(lly - m)} m {F(urx)} {F(lly)} l S",
        # верхний левый
        f"{F(llx - m)} {F(ury)} m {F(llx)} {F(ury)} l S",
        f"{F(llx)} {F(ury)} m {F(llx)} {F(ury + m)} l S",
        # верхний правый
        f"{F(urx)} {F(ury)} m {F(urx + m)} {F(ury)} l S",
        f"{F(urx)} {F(ury)} m {F(urx)} {F(ury + m)} l S",
        "Q",
    ]
    return ("\n".join(ops) + "\n").encode("ascii")


def _append_content_stream(pdf: pikepdf.Pdf, page, data: bytes) -> None:
    stream = pikepdf.Stream(pdf, data)
    key = Name("/Contents")
    cur = page.obj.get(key)
    if cur is None:
        page.obj[key] = stream
    elif isinstance(cur, pikepdf.Array):
        cur.append(stream)
    else:
        page.obj[key] = pikepdf.Array([cur, stream])


def adjust_bleed_pdf(
    input_path: str,
    output_path: str,
    bleed_mm: float = 3.0,
    *,
    add_crop_marks: bool = False,
    crop_mark_len_mm: float = 3.0,
) -> None:
    bleed_pt = bleed_mm * MM_TO_PT
    mark_pt = max(0.0, crop_mark_len_mm) * MM_TO_PT

    with pikepdf.open(input_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            trim = page.get("/TrimBox", page.get("/MediaBox"))
            if trim is None:
                logging.warning(f"Стр. {i}: Нет боксов. Пропуск.")
                continue

            llx, lly, urx, ury = [float(v) for v in trim]

            new_bounds = [llx - bleed_pt, lly - bleed_pt, urx + bleed_pt, ury + bleed_pt]
            trim_bounds = [llx, lly, urx, ury]

            page.MediaBox = new_bounds
            page.BleedBox = new_bounds
            page.TrimBox = trim_bounds
            page.CropBox = new_bounds

            if add_crop_marks and mark_pt > 0:
                ops = _crop_mark_ops(llx, lly, urx, ury, mark_pt)
                _append_content_stream(pdf, page, ops)

        pdf.save(output_path, fix_metadata_version=True)
        logging.info(f"✅ Обработано {len(pdf.pages)} стр. → {output_path}")


def apply_white_margins_pdf(
    input_path: str,
    output_path: str,
    margin_mm: float = 2.0,
) -> None:
    """Уменьшить контент и добавить белые поля внутри TrimBox (режим «белые поля»)."""
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError("PyMuPDF нужен для белых полей") from e

    margin_pt = margin_mm * MM_TO_PT
    doc = fitz.open(input_path)
    out = fitz.open()

    for pno in range(doc.page_count):
        src = doc[pno]
        trim = src.trimbox if src.trimbox else src.rect
        bleed = src.bleedbox if src.bleedbox else src.mediabox
        inner = fitz.Rect(
            trim.x0 + margin_pt,
            trim.y0 + margin_pt,
            trim.x1 - margin_pt,
            trim.y1 - margin_pt,
        )
        if inner.width <= 0 or inner.height <= 0:
            inner = trim

        page = out.new_page()
        page.set_mediabox(bleed)
        page.set_cropbox(bleed)
        page.set_trimbox(trim)
        if src.bleedbox:
            page.set_bleedbox(src.bleedbox)
        page.draw_rect(trim, color=(1, 1, 1), fill=(1, 1, 1))
        page.show_pdf_page(inner, doc, pno, clip=trim, keep_proportion=True)

    out.save(output_path, garbage=4, deflate=True)
    out.close()
    doc.close()
    logging.info("✅ Белые поля %.1f мм → %s", margin_mm, output_path)


def apply_mirror_bleed_pdf(
    input_path: str,
    output_path: str,
    bleed_mm: float = 2.0,
    *,
    dpi: int = 300,
) -> None:
    """Вылеты зеркалированием краёв TrimBox (растр, PyMuPDF + Pillow)."""
    import io

    try:
        import fitz
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("PyMuPDF и Pillow нужны для зеркальных вылетов") from e

    bleed_pt = bleed_mm * MM_TO_PT
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)

    def pt_to_px(pt: float) -> int:
        return max(1, int(round(pt / 72.0 * dpi)))

    def mirror_strip(strip: Image.Image, target_w: int, target_h: int, flip) -> Image.Image:
        if strip.width != target_w or strip.height != target_h:
            strip = strip.resize((target_w, target_h), Image.Resampling.LANCZOS)
        return strip.transpose(flip)

    doc = fitz.open(input_path)
    out = fitz.open()

    for pno in range(doc.page_count):
        src = doc[pno]
        trim = src.trimbox if src.trimbox else src.rect
        existing = src.bleedbox if src.bleedbox else src.mediabox

        left_pt = max(bleed_pt, trim.x0 - existing.x0)
        right_pt = max(bleed_pt, existing.x1 - trim.x1)
        bottom_pt = max(bleed_pt, trim.y0 - existing.y0)
        top_pt = max(bleed_pt, existing.y1 - trim.y1)
        media = fitz.Rect(
            trim.x0 - left_pt,
            trim.y0 - bottom_pt,
            trim.x1 + right_pt,
            trim.y1 + top_pt,
        )

        pix = src.get_pixmap(matrix=mat, clip=trim, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        w, h = img.size
        left_px = pt_to_px(left_pt)
        right_px = pt_to_px(right_pt)
        bottom_px = pt_to_px(bottom_pt)
        top_px = pt_to_px(top_pt)

        canvas = Image.new(
            "RGB",
            (w + left_px + right_px, h + bottom_px + top_px),
            (255, 255, 255),
        )
        canvas.paste(img, (left_px, bottom_px))

        top_strip = mirror_strip(
            img.crop((0, 0, w, min(h, top_px))), w, top_px, Image.FLIP_TOP_BOTTOM
        )
        canvas.paste(top_strip, (left_px, 0))
        bottom_strip = mirror_strip(
            img.crop((0, max(0, h - bottom_px), w, h)), w, bottom_px, Image.FLIP_TOP_BOTTOM
        )
        canvas.paste(bottom_strip, (left_px, bottom_px + h))
        left_strip = mirror_strip(
            img.crop((0, 0, min(left_px, w), h)), left_px, h, Image.FLIP_LEFT_RIGHT
        )
        canvas.paste(left_strip, (0, bottom_px))
        right_strip = mirror_strip(
            img.crop((max(0, w - right_px), 0, w, h)), right_px, h, Image.FLIP_LEFT_RIGHT
        )
        canvas.paste(right_strip, (left_px + w, bottom_px))

        tl = mirror_strip(
            img.crop((0, 0, min(left_px, w), min(top_px, h))), left_px, top_px, Image.ROTATE_180
        )
        canvas.paste(tl, (0, 0))
        tr = mirror_strip(
            img.crop((max(0, w - right_px), 0, w, min(top_px, h))), right_px, top_px, Image.ROTATE_180
        )
        canvas.paste(tr, (left_px + w, 0))
        bl = mirror_strip(
            img.crop((0, max(0, h - bottom_px), min(left_px, w), h)), left_px, bottom_px, Image.ROTATE_180
        )
        canvas.paste(bl, (0, bottom_px + h))
        br = mirror_strip(
            img.crop((max(0, w - right_px), max(0, h - bottom_px), w, h)),
            right_px,
            bottom_px,
            Image.ROTATE_180,
        )
        canvas.paste(br, (left_px + w, bottom_px + h))

        page = out.new_page(width=media.width, height=media.height)
        page.set_mediabox(media)
        page.set_cropbox(media)
        page.set_trimbox(trim)
        page.set_bleedbox(media)
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        page.insert_image(media, stream=buf.getvalue())

    out.save(output_path, garbage=4, deflate=True)
    out.close()
    doc.close()
    logging.info("✅ Зеркальные вылеты %.1f мм → %s", bleed_mm, output_path)
