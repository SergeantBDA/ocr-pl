@echo off
setlocal
call venv\Scripts\activate
rem  -p: процессов, -t: потоков на процесс (актеры — кооперативно)
python -m dramatiq app.workers -p 1 -t 4
