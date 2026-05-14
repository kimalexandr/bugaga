import pikepdf
import logging

MM_TO_PT = 72.0 / 25.4

def adjust_bleed_pdf(input_path: str, output_path: str, bleed_mm: float = 3.0) -> None:
    bleed_pt = bleed_mm * MM_TO_PT
    
    with pikepdf.open(input_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            # TrimBox или fallback на MediaBox
            trim = page.get("/TrimBox", page.get("/MediaBox"))
            if trim is None:
                logging.warning(f"Стр. {i}: Нет боксов. Пропуск.")
                continue

            llx, lly, urx, ury = [float(v) for v in trim]

            # Новые границы с вылетом
            new_bounds = [llx - bleed_pt, lly - bleed_pt, urx + bleed_pt, ury + bleed_pt]
            
            page.MediaBox = pikepdf.Array(pdf, new_bounds)
            page.BleedBox = pikepdf.Array(pdf, new_bounds)
            page.TrimBox  = pikepdf.Array(pdf, [llx, lly, urx, ury])
            page.CropBox  = pikepdf.Array(pdf, new_bounds)

        pdf.save(output_path, fix_metadata_version=True)
        logging.info(f"✅ Обработано {len(pdf.pages)} стр. → {output_path}")