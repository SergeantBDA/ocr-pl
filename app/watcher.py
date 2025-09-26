import os
import time
import stat
import threading
import pathlib
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler, FileCreatedEvent, FileMovedEvent,
    DirCreatedEvent, DirMovedEvent
)
from loguru import logger
from dotenv import load_dotenv

from .utils import IN_DIR, is_ready, ext_lower
from .workers import ocr_file

load_dotenv()

SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

# Настройки (можно задать в .env)
DIR_SETTLE_SEC     = float(os.getenv("DIR_SETTLE_SEC", "2"))    # задержка перед сканом созданной папки
FILE_WAIT_RETRIES  = int(os.getenv("FILE_WAIT_RETRIES", "40"))  # 40*0.5 = ~20 секунд
FILE_WAIT_STEP     = float(os.getenv("FILE_WAIT_STEP", "0.5"))
FOLLOW_REPARSE     = os.getenv("FOLLOW_REPARSE", "0") == "1"    # если 1 — заходить в reparse (джанкшены/симлинки)
# простые исключения каталогов по имени (без учёта регистра)
EXCLUDE_DIRS = {s.strip().lower() for s in os.getenv(
    "EXCLUDE_DIRS",
    "$recycle.bin,System Volume Information,__pycache__,.git"
).split(",") if s.strip()}

_seen_paths: set[str] = set()
_seen_lock = threading.Lock()

def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))

def _mark_enqueued_once(path: str) -> bool:
    norm = _norm(path)
    with _seen_lock:
        if norm in _seen_paths:
            return False
        _seen_paths.add(norm)
        if len(_seen_paths) > 50000:
            _seen_paths.clear()
        return True

def _wait_until_ready(path: str) -> bool:
    for _ in range(FILE_WAIT_RETRIES):
        if is_ready(path, wait_sec=FILE_WAIT_STEP):
            return True
        time.sleep(FILE_WAIT_STEP)
    return False

def _enqueue_file(path: str):
    ext = ext_lower(path)
    if ext not in SUPPORTED:
        logger.debug(f"Skip unsupported: {path}")
        return
    if not os.path.isfile(path):
        logger.debug(f"Skip non-file: {path}")
        return
    if not _mark_enqueued_once(path):
        logger.debug(f"Duplicate skipped: {path}")
        return
    if not _wait_until_ready(path):
        logger.warning(f"File not ready, skipping: {path}")
        return
    logger.info(f"Enqueue: {path}")
    ocr_file.send(path)

# --- Работа с reparse points (симлинки/джанкшены) на Windows ---

def is_reparse_point(p: Path) -> bool:
    """
    Возвращает True, если каталог/файл — reparse point (включая джанкшены и симлинки).
    На не-Windows всегда False (безопасно).
    """
    try:
        st = os.stat(str(p), follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False
    except OSError:
        # Если не смогли прочитать — не рискуем, считаем reparse
        return True

def _should_skip_dir(parent: str, name: str) -> bool:
    """Решаем, заходить ли в подкаталог name под parent."""
    full = Path(parent) / name
    # исключения по имени
    if name.lower() in EXCLUDE_DIRS:
        return True
    # симлинк?
    try:
        if full.is_symlink():
            return not FOLLOW_REPARSE
    except OSError:
        return True
    # reparse (включает джанкшены/mount points)
    try:
        if is_reparse_point(full):
            return not FOLLOW_REPARSE
    except OSError:
        return True
    return False

def _walk_onerror(err: OSError):
    # os.walk передаст исключение сюда — логируем и продолжаем обход
    # err.filename может быть None; используем repr(err) на всякий
    target = getattr(err, "filename", None) or "unknown"
    logger.warning(f"os.walk error at {target}: {err!r}")

def _enqueue_tree(root: str):
    """
    Рекурсивный обход с pruning:
    - не заходим в reparse-каталоги (симлинки/джанкшены) если FOLLOW_REPARSE=0,
    - игнор EXCLUDE_DIRS,
    - безопасная обработка PermissionError через onerror.
    """
    if not os.path.isdir(root):
        return

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False, onerror=_walk_onerror):
        # Pruning: выкидываем нежелательные подкаталоги до входа в них
        kept: list[str] = []
        for d in dirnames:
            try:
                if _should_skip_dir(dirpath, d):
                    logger.info(f"Skip directory: {os.path.join(dirpath, d)}")
                    continue
                kept.append(d)
            except Exception as e:
                logger.warning(f"Skip directory (error): {os.path.join(dirpath, d)} :: {e}")
        dirnames[:] = kept  # важно — влияет на дальнейшую рекурсию os.walk

        # файлы текущей папки
        for name in filenames:
            _enqueue_file(os.path.join(dirpath, name))

# --- Watchdog обработчик событий ---

class Handler(FileSystemEventHandler):
    def on_created(self, event):
        try:
            if isinstance(event, DirCreatedEvent):
                logger.info(f"Directory created: {event.src_path} — will scan after {DIR_SETTLE_SEC}s")
                threading.Thread(target=self._deferred_scan, args=(event.src_path,), daemon=True).start()
            elif isinstance(event, FileCreatedEvent):
                self._maybe_enqueue_file(event.src_path)
        except Exception as e:
            logger.exception(f"on_created error for {getattr(event, 'src_path', '?')}: {e}")

    def on_moved(self, event):
        try:
            if isinstance(event, DirMovedEvent):
                logger.info(f"Directory moved to: {event.dest_path} — will scan after {DIR_SETTLE_SEC}s")
                threading.Thread(target=self._deferred_scan, args=(event.dest_path,), daemon=True).start()
            elif isinstance(event, FileMovedEvent):
                self._maybe_enqueue_file(event.dest_path)
        except Exception as e:
            logger.exception(f"on_moved error for {getattr(event, 'dest_path', '?')}: {e}")

    def _maybe_enqueue_file(self, path: str):
        _enqueue_file(path)

    def _deferred_scan(self, dir_path: str):
        time.sleep(DIR_SETTLE_SEC)
        _enqueue_tree(dir_path)

def initial_recursive_scan():
    if not os.path.isdir(IN_DIR):
        return
    logger.info(f"Initial recursive scan of: {IN_DIR}")
    _enqueue_tree(IN_DIR)

def main():
    pathlib.Path(IN_DIR).mkdir(parents=True, exist_ok=True)
    initial_recursive_scan()  # разовый скан уже имеющихся файлов

    logger.info(f"Watching (recursive): {IN_DIR}  | FOLLOW_REPARSE={int(FOLLOW_REPARSE)}")
    event_handler = Handler()
    observer = Observer()
    observer.schedule(event_handler, IN_DIR, recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
