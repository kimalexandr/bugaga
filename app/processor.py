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
