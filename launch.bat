@echo off
REM Corpus Intel — Windows launcher
REM Starts FastAPI on http://127.0.0.1:8788

cd /d "%~dp0"
python -m uvicorn corpus_intel.web_server:app --host 127.0.0.1 --port 8788
