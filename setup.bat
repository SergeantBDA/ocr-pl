@echo off
setlocal
python -m venv venv
call venv\Scripts\pip install --upgrade pip
call venv\Scripts\pip install -r requirements.txt
echo Setup done.
