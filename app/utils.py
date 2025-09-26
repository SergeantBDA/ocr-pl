import os, re, time, hashlib, pathlib, shutil
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

IN_DIR    = os.getenv("IN_DIR", r"D:\TEMP\DocOCR\in")
OUT_DIR   = os.getenv("OUT_DIR", r"D:\TEMP\DocOCR\out")
ERR_DIR   = os.getenv("ERROR_DIR", r"D:\TEMP\DocOCR\err")
LOG_DIR   = os.getenv("LOG_DIR", r"D:\TEMP\DocOCR\logs")

# Создать каталоги при импорте
pathlib.Path(IN_DIR).mkdir(parents=True, exist_ok=True)
pathlib.Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
pathlib.Path(ERR_DIR).mkdir(parents=True, exist_ok=True)
pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

logger.add(os.path.join(LOG_DIR, "dococr.log"), rotation="10 MB", retention=10)

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

def safe_basename(p: str) -> str:
    base = os.path.basename(p)
    return SAFE_NAME_RE.sub("_", base)

def unique_stem(p: str) -> str:
    stem = pathlib.Path(p).stem
    h = hashlib.sha1(p.encode("utf-8")).hexdigest()[:8]
    return f"{stem}_{h}"

def move_to_err(src: str, reason: str) -> None:
    try:
        target = os.path.join(ERR_DIR, safe_basename(src))
        shutil.move(src, target)
        logger.error(f"Moved to ERR: {src} -> {target}. Reason: {reason}")
    except Exception as e:
        logger.exception(f"Failed to move to ERR: {src}. Reason: {reason}. Error: {e}")

def is_ready(file_path: str, wait_sec: float = 0.5) -> bool:
    """
    Простейшая проверка «дозаписи»: размер файла не меняется в течение wait_sec.
    """
    try:
        s1 = os.path.getsize(file_path)
        time.sleep(wait_sec)
        s2 = os.path.getsize(file_path)
        return s1 == s2
    except FileNotFoundError:
        return False

def ext_lower(p: str) -> str:
    return pathlib.Path(p).suffix.lower()
