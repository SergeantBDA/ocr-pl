# app/workers.py
import os
import io
import re
import concurrent.futures
from typing import List, Optional, Tuple

import fitz  # PyMuPDF
from PIL import Image
import pytesseract
from loguru import logger
import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import TimeLimit
from dotenv import load_dotenv

from .utils import IN_DIR, OUT_DIR, move_to_err, unique_stem, ext_lower

load_dotenv()

# --------------------------
# Настройки из .env
# --------------------------
redis_url     = os.getenv("REDIS_URL", "redis://localhost:6379/0")
tesseract_exe = os.getenv("TESSERACT_EXE", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
if os.path.exists(tesseract_exe):
    pytesseract.pytesseract.tesseract_cmd = tesseract_exe

OCR_LANG         = os.getenv("OCR_LANG", "eng")
OUTPUT_TXT       = os.getenv("OUTPUT_TXT", "1") == "1"
OUTPUT_PDF       = os.getenv("OUTPUT_PDF", "1") == "1"
TESSERACT_CONFIG = os.getenv("TESSERACT_CONFIG", "--psm 6")
OCR_THREADS      = int(os.getenv("OCR_THREADS", "2"))
OCR_DPI          = int(os.getenv("OCR_DPI", "300"))     # DPI для OCR только скан-страниц
TEXT_MIN_CHARS   = int(os.getenv("TEXT_MIN_CHARS", "16"))  # порог «есть текст на странице»

SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

# --------------------------
# Dramatiq broker + middleware
# ВАЖНО: time_limit в миллисекундах!
# --------------------------
broker = RedisBroker(url=redis_url)
dramatiq.set_broker(broker)
broker.add_middleware(TimeLimit(time_limit=8 * 60 * 60 * 1000))  # 8 часов

# --------------------------
# Текстовые утилиты
# --------------------------
_ws_re        = re.compile(r"[ \t\u00A0]+")
_hyphen_re    = re.compile(r"(\w)-\s*\n(\w)")
_single_nl_re = re.compile(r"(?<!\n)\n(?!\n)")   # одиночный \n, не часть \n\n
_multi_nl_re  = re.compile(r"\n{3,}")            # 3+ переводов → 2

def preprocess_text_layer(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _hyphen_re.sub(r"\1\2", text)        # убираем переносы по дефису
    text = _single_nl_re.sub(" ", text)         # одиночный \n превращаем в пробел
    text = _ws_re.sub(" ", text)                # сжимаем пробелы (вкл. NBSP)
    text = _multi_nl_re.sub("\n", text)       # абзацы нормализуем к двум \n
    return text.strip()

def extract_text_from_page(page) -> str:
    parts = []
    # blocks: (x0, y0, x1, y1, text, block_no, block_type)
    for x0, y0, x1, y1, txt, bno, btype in page.get_text("blocks", sort=True):
        if btype != 0:      # 0 = текст
            continue
        parts.append(preprocess_text_layer(txt))
    # Между блоками оставляем пустую строку
    return "\n\n".join([p for p in parts if p])

def page_has_text(page: "fitz.Page", min_chars: int = TEXT_MIN_CHARS) -> bool:
    txt = page.get_text("text", sort=True)
    if len(re.sub(r"\s+", "", txt)) >= min_chars:
        return True
    d = page.get_text("rawdict")
    for b in d.get("blocks", []):
        if b.get("type") == 0:
            for line in b.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text") and len(span["text"].strip()) > 0:
                        return True
    return False

def render_page_to_image(pdf_path: str, page_number: int, dpi: int = OCR_DPI) -> Image.Image:
    with fitz.open(pdf_path) as doc:
        page = doc.load_page(page_number)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return Image.open(io.BytesIO(pix.tobytes("png")))

def ocr_image_to_text_and_pdf(img: Image.Image) -> Tuple[str, bytes]:
    txt = pytesseract.image_to_string(img, lang=OCR_LANG, config=TESSERACT_CONFIG)
    pdf_bytes = pytesseract.image_to_pdf_or_hocr(img, extension="pdf", lang=OCR_LANG, config=TESSERACT_CONFIG)
    return txt, pdf_bytes

# --------------------------
# Помощники для зеркалирования структуры IN_DIR → OUT_DIR
# --------------------------
def _resolve_out_dir_for(file_path: str) -> str:
    """
    Возвращает каталог внутри OUT_DIR, соответствующий относительному пути файла от IN_DIR.
    Если файл не под IN_DIR, вернёт просто OUT_DIR.
    """
    try:
        rel = os.path.relpath(os.path.dirname(file_path), IN_DIR)
        if rel.startswith(".."):
            rel = ""  # файл вне IN_DIR
    except Exception:
        rel = ""
    out_dir = os.path.join(OUT_DIR, rel) if rel else OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    return out_dir

def _make_out_paths(file_path: str) -> Tuple[str, str]:
    """
    Строит пути для TXT и PDF в нужной подпапке OUT_DIR,
    имя файла — unique_stem(file_path) + соответствующее расширение.
    """
    out_dir = _resolve_out_dir_for(file_path)
    base = unique_stem(file_path)
    return (
        os.path.join(out_dir, f"{base}.txt"),
        os.path.join(out_dir, f"{base}.pdf"),
    )

# --------------------------
# Основной актор
# --------------------------
@dramatiq.actor(max_retries=0, time_limit=8 * 60 * 60 * 1000)  # 8 часов, миллисекунды
def ocr_file(file_path: str):
    """
    PDF:
      - если на странице есть текст → берём fitz-текст и копируем страницу «как есть» в результат;
      - если страницы-сканы → OCR только для них, вставляем OCR-страницы в выходной PDF.
    Изображения: полный OCR.
    Выход сохраняется в OUT_DIR с сохранением относительной структуры от IN_DIR.
    """
    try:
        ext = ext_lower(file_path)
        if ext not in SUPPORTED:
            move_to_err(file_path, f"Unsupported extension: {ext}")
            return

        out_txt_path, out_pdf_path = _make_out_paths(file_path)

        if ext == ".pdf":
            logger.info(f"OCR PDF (hybrid): {file_path}")

            # 1) Определяем страницы с текстом и собираем текстовый слой
            with fitz.open(file_path) as src:
                page_count = src.page_count
                logger.info(f"PDF pages: {page_count}")

                text_per_page: List[Optional[str]] = [None] * page_count
                is_scan_page: List[bool] = [False] * page_count

                for n in range(page_count):
                    p = src.load_page(n)
                    if page_has_text(p):
                        text_per_page[n] = extract_text_from_page(p)
                        is_scan_page[n] = False
                    else:
                        is_scan_page[n] = True  # позже сделаем OCR

            # 2) OCR только скан-страниц (параллельно)
            ocr_results: dict[int, Tuple[str, bytes]] = {}
            scan_indices = [i for i, flag in enumerate(is_scan_page) if flag]
            if scan_indices:
                logger.info(f"Scanning pages via OCR: {len(scan_indices)}")
                def run_ocr(n: int) -> Tuple[int, str, bytes]:
                    img = render_page_to_image(file_path, n, dpi=OCR_DPI)
                    txt, pdf_bytes = ocr_image_to_text_and_pdf(img)
                    return n, txt, pdf_bytes

                with concurrent.futures.ThreadPoolExecutor(max_workers=OCR_THREADS) as pool:
                    futures = [pool.submit(run_ocr, n) for n in scan_indices]
                    for fut in concurrent.futures.as_completed(futures):
                        n, txt, pdf_bytes = fut.result()
                        ocr_results[n] = (txt, pdf_bytes)
                        text_per_page[n] = preprocess_text_layer(txt)

            # 3) TXT
            if OUTPUT_TXT:
                os.makedirs(os.path.dirname(out_txt_path), exist_ok=True)
                with open(out_txt_path, "w", encoding="utf-8") as f:
                    for n, page_text in enumerate(text_per_page):
                        if n > 0:
                            f.write("\n\n")
                        f.write(page_text or "")

            # 4) PDF
            if OUTPUT_PDF:
                os.makedirs(os.path.dirname(out_pdf_path), exist_ok=True)
                with fitz.open(file_path) as src, fitz.open() as outdoc:
                    for n in range(src.page_count):
                        if not is_scan_page[n]:
                            # Копируем исходную страницу (текстовый слой сохранится)
                            outdoc.insert_pdf(src, from_page=n, to_page=n)
                        else:
                            # Вставляем одностраничный OCR-PDF
                            txt_pdf = ocr_results[n][1]
                            with fitz.open(stream=txt_pdf, filetype="pdf") as ocr_page_doc:
                                outdoc.insert_pdf(ocr_page_doc)
                    outdoc.save(out_pdf_path)

            logger.info(f"OCR done: {file_path} -> {os.path.dirname(out_txt_path)}")

        else:
            # --------------------------
            # Изображение
            # --------------------------
            logger.info(f"OCR Image: {file_path}")
            img = Image.open(file_path)
            txt, pdf_bytes = ocr_image_to_text_and_pdf(img)
            txt = preprocess_text_layer(txt)

            if OUTPUT_TXT:
                os.makedirs(os.path.dirname(out_txt_path), exist_ok=True)
                with open(out_txt_path, "w", encoding="utf-8") as f:
                    f.write(txt)

            if OUTPUT_PDF:
                os.makedirs(os.path.dirname(out_pdf_path), exist_ok=True)
                with open(out_pdf_path, "wb") as pf:
                    pf.write(pdf_bytes)

            logger.info(f"OCR done: {file_path} -> {os.path.dirname(out_txt_path)}")

    except Exception as e:
        logger.exception(f"OCR failed: {file_path}: {e}")
        move_to_err(file_path, str(e))
