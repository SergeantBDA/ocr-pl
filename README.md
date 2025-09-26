# DocOCR (Windows Server 2022 · Dramatiq + Memurai/Redis · без контейнеров)

Простой и надёжный конвейер распознавания документов под **Windows Server 2022 Standard**.
Складываете файлы в `in/` — результаты появляются в `out/` (с сохранением структуры подкаталогов).
Очередь задач — **Dramatiq** с брокером **Memurai/Redis**. OCR — **Tesseract**. PDF — **PyMuPDF (fitz)**.
Watcher — рекурсивный, устойчивый к «недописанным» файлам и аккуратный с reparse‑точками (симлинки/джанкшены).  
Worker — гибридный: использует текстовый слой PDF, OCR делает только для сканов.

---

## 🚀 Возможности

- 📂 Наблюдение за `in/` **рекурсивно** по всем вложениям.
- 🧠 **Гибридный OCR для PDF**:  
  • если страница содержит текстовый слой → извлекаем текст через `fitz` и копируем страницу **без растринга**;  
  • если страница — скан → рендер одной страницы и OCR через Tesseract (точечно).  
- 📝 Выход: `*.txt` и/или **searchable** `*.pdf`.
- 🧭 **Зеркалирование структуры**: `in\dept\2024\file.pdf → out\dept\2024\file_<hash>.txt/.pdf`.
- 🧱 Безопасный обход: **по умолчанию не заходим** в reparse‑папки (симлинки/джанкшены).
- 🔁 Дедупликация постановок + ожидание «дозаписи» файла.
- 🧩 Запуск watcher/worker как служб Windows (через **NSSM**).

---

## 🧩 Архитектура

```
Файл попал в IN_DIR (включая вложенные папки)
   ↓ (watchdog)
watcher → ocr_file.send(path)  ──────────────→ Memurai/Redis (очередь Dramatiq)
                                               ↓
                                        worker читает сообщение
                                               ↓
                         PDF: текстовые страницы → fitz, сканы → Tesseract
                         Image: Tesseract целиком
                                               ↓
                OUT_DIR (зеркальная структура) / ERR_DIR (ошибки) / LOG_DIR (логи)
```

---

## 🧾 Требования

- **Windows Server 2022 Standard** (подойдёт и Windows 10/11 для теста).
- **Python 3.11 (x64)**.
- **Tesseract OCR** (и нужные языки, напр. `rus`).
- **Memurai** (рекомендуется) или Redis (через WSL).  
- **NSSM** — опционально для служб.

---

## 📦 Структура проекта

```
C:\DocOCR\
  in\
  out\
  err\
  logs\
  venv\
  app\
    __init__.py
    utils.py
    watcher.py        # рекурсивный, с onerror, пропуском reparse (по умолчанию)
    workers.py        # гибрид: fitz-текст + OCR только для сканов; зеркалирование структуры
  requirements.txt
  .env
  start_worker.bat
  start_watcher.bat
  setup.bat
```

---

## 🔧 Установка

1) **Tesseract** → `C:\Program Files\Tesseract-OCR\`  
   Проверьте:
   ```bat
   "C:\Program Files\Tesseract-OCR\tesseract.exe" --version
   ```

2) **Memurai** (Developer Edition) — ставится как служба `Memurai` (порт `6379`).  
   Проверка:
   ```powershell
   Test-NetConnection localhost -Port 6379
   "C:\Program Files\Memurai\memurai-cli.exe" -h localhost -p 6379 ping
   ```
   Ожидается `TcpTestSucceeded : True` и `PONG`.

3) **Python‑зависимости**:
   ```bat
   C:\DocOCR\setup.bat
   ```

---

## ⚙️ Конфигурация (`.env`)

```ini
# Брокер Dramatiq
REDIS_URL=redis://localhost:6379/0

# Каталоги
IN_DIR=C:\DocOCR\in
OUT_DIR=C:\DocOCR\out
ERROR_DIR=C:\DocOCR\err
LOG_DIR=C:\DocOCR\logs

# Tesseract
TESSERACT_EXE=C:\Program Files\Tesseract-OCR\tesseract.exe
OCR_LANG=eng+rus
TESSERACT_CONFIG=--psm 6           # опционально: меньше ложных переносов в OCR

# Выходные форматы
OUTPUT_TXT=1
OUTPUT_PDF=1

# Производительность
OCR_THREADS=2                      # потоки OCR для скан‑страниц
OCR_DPI=300                        # DPI рендеринга только для сканов
TEXT_MIN_CHARS=16                  # порог "на странице есть текст"

# Watcher (устойчивость и рекурсия)
DIR_SETTLE_SEC=2                   # задержка перед сканом созданной папки
FILE_WAIT_RETRIES=40               # 40*0.5 ≈ 20 сек ожидания "дозаписи"
FILE_WAIT_STEP=0.5
FOLLOW_REPARSE=0                   # 0 — не заходить в симлинки/джанкшены; 1 — заходить (осторожно)
EXCLUDE_DIRS=$recycle.bin,System Volume Information,__pycache__,.git
```

> Убедитесь, что в `...Tesseract-OCR\tessdata\` лежат `eng.traineddata`, `rus.traineddata` и др. нужные языки.

---

## ▶️ Запуск (локально)

Откройте два окна:

```bat
C:\DocOCR\start_worker.bat
C:\DocOCR\start_watcher.bat
```

- Worker поднимет Dramatiq и подключится к Memurai/Redis.  
- Watcher начнёт следить за `IN_DIR` **рекурсивно** и выполнит стартовый скан уже имеющихся файлов.

Логи: `C:\DocOCR\logs\dococr.log` (и `worker.out/err.log`, `watcher.out/err.log`, если через NSSM).

---

## 🧰 Запуск как службы (NSSM)

**Worker**
```bat
C:\Tools\nssm\nssm.exe install DocOCR-Worker "C:\DocOCR\venv\Scripts\python.exe" -m dramatiq app.workers -p 1 -t 2
C:\Tools\nssm\nssm.exe set DocOCR-Worker AppDirectory C:\DocOCR
C:\Tools\nssm\nssm.exe set DocOCR-Worker AppStdout C:\DocOCR\logs\worker.out.log
C:\Tools\nssm\nssm.exe set DocOCR-Worker AppStderr C:\DocOCR\logs\worker.err.log
C:\Tools\nssm\nssm.exe start DocOCR-Worker
```

**Watcher**
```bat
C:\Tools\nssm\nssm.exe install DocOCR-Watcher "C:\DocOCR\venv\Scripts\python.exe" -m app.watcher
C:\Tools\nssm\nssm.exe set DocOCR-Watcher AppDirectory C:\DocOCR
C:\Tools\nssm\nssm.exe set DocOCR-Watcher AppStdout C:\DocOCR\logs\watcher.out.log
C:\Tools\nssm\nssm.exe set DocOCR-Watcher AppStderr C:\DocOCR\logs\watcher.err.log
C:\Tools\nssm\nssm.exe start DocOCR-Watcher
```

> Задайте учётку службы и автозапуск при необходимости.

---

## 🧠 Детали реализации

### Watcher (`app/watcher.py`)
- `watchdog` + `os.walk(topdown=True, followlinks=False, onerror=...)`.
- **Pruning**: пропускаем каталоги из `EXCLUDE_DIRS` и любые reparse‑папки (симлинки/джанкшены) при `FOLLOW_REPARSE=0`.
- Дедупликация задач, ожидание стабильного размера файла, стартовый рекурсивный скан.

### Worker (`app/workers.py`)
- Актор `ocr_file` у Dramatiq (`time_limit` в **мс**; по умолчанию 8 часов через декоратор и middleware).
- **Гибрид PDF**:  
  • Текстовые страницы → `fitz.get_text("blocks", sort=True)` + предобработка (`preprocess_text_layer`), копирование страницы в выходной PDF **без растринга**.  
  • Скан‑страницы → рендер одной страницы (`OCR_DPI`) → `pytesseract` (`OCR_LANG`, `TESSERACT_CONFIG`) → вставка single‑page OCR‑PDF в итог.  
- Изображения: OCR через Tesseract целиком.
- **Зеркалирование путей**: `OUT_DIR / relpath(from=IN_DIR)`.

### Нормализация текста (`preprocess_text_layer`)
- Склейка переносов по дефису.
- Превращение **одиночного** `\n` в пробел, сохранение абзацев (`\n\n`).  
- Нормализация пробелов (вкл. NBSP).

---

## 🧪 Проверка

1. Проверить брокер:
   ```powershell
   Test-NetConnection localhost -Port 6379
   "C:\Program Files\Memurai\memurai-cli.exe" -h localhost -p 6379 ping
   ```
2. Положить `PDF/JPG` в `in\` (или `in\dept\2024\`).
3. Убедиться, что в `out\dept\2024\` появились `*.txt` и/или `*.pdf`.
4. Ошибочные файлы окажутся в `err\` — подробности в `logs\dococr.log`.

---

## 🛠️ Тюнинг и советы

- **Время**: `time_limit` указывайте в **миллисекундах** (пример: `8 * 60 * 60 * 1000`).
- **Скорость/качество OCR**:  
  • `OCR_THREADS` — 2–4;  
  • `OCR_DPI` — 300 (200 быстрее, 400–600 лучше для мелкого шрифта);  
  • `TESSERACT_CONFIG="--psm 6"` (или 4 — для много‑колоночного текста).
- **Детектор текстового слоя**: `TEXT_MIN_CHARS` подстраивайте под ваши PDF.
- **Большие PDF**: подумайте о шардировании (ставить в очередь батчи страниц).

---

## 🩺 Разбор типичных проблем

- **В out/ ничего нет**  
  • Разные корни у watcher/worker (проверьте `cd` в `.bat` и пути в `.env`).  
  • Memurai не запущен или порт занят.  
  • Права на папки/службу.

- **`TimeLimitExceeded` почти сразу**  
  • Лимит задан в секундах вместо миллисекунд. Пример правильного: `8 * 60 * 60 * 1000`.

- **Текст «лесенкой» (каждое слово с новой строки)**  
  • Это артефакт PDF. В `preprocess_text_layer` превращаем одиночный `\n` в пробел и склеиваем дефисы.  
  • Для OCR добавьте `TESSERACT_CONFIG=--psm 6`.

- **`TesseractError` / пустой OCR**  
  • Проверьте наличие `*.traineddata` в `tessdata`.  
  • Подстройте `OCR_DPI`/`--psm`.  

- **Очередь забита**  
  ```bat
  "C:\Program Files\Memurai\memurai-cli.exe" -h localhost -p 6379 LRANGE dramatiq:queue:default 0 -1
  ```

---

## 📜 Лицензия

MIT/Apache 2.0

---

## 🧭 Шпаргалка (быстрый старт)

```bat
:: 1) Зависимости
C:\DocOCR\setup.bat

:: 2) Проверка Memurai
Test-NetConnection localhost -Port 6379
"C:\Program Files\Memurai\memurai-cli.exe" -h localhost -p 6379 ping

:: 3) Запуск
C:\DocOCR\start_worker.bat
C:\DocOCR\start_watcher.bat

:: 4) Тест: скопировать PDF в C:\DocOCR\in\dept\2024\
:: 5) Проверить C:\DocOCR\out\dept\2024\
```
