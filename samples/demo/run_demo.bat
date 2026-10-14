@echo off
chcp 65001 >nul
rem 발표용 데모 — 가짜 메일·가짜 마스터·샘플 소스로 챗봇을 띄운다.
rem config_local.py 는 건드리지 않는다. 설정은 이 창에서만 환경변수로 준다.
rem 띄운 뒤 브라우저에서 "오늘 인터페이스 오류 확인해줘".
rem
rem   samples\demo\run_demo.bat
rem
rem Ollama 주소·모델이 다르면 미리 set 해 두거나 아래 기본값을 고친다.
cd /d "%~dp0..\.."
if not defined OLLAMA_HOST set OLLAMA_HOST=http://localhost:11434
if not defined CHAT_MODEL set CHAT_MODEL=qwen3.5:4b
if not defined JUDGE_MODEL set JUDGE_MODEL=%CHAT_MODEL%
rem 비워 두면 qwen3.5:4b 가 사고 과정만 쓰고 빈 답을 내는 일이 있었다(실측). 끄면 빠르고 안정적이다.
if not defined CHAT_REASONING set CHAT_REASONING=false

set MAIL_BACKEND=eml
set MAIL_EML_DIR=out\demo_mail
set IFERR_MASTER_FILE=samples\demo\if_master.json
rem 소스 루트 두 개 — 여러 루트를 함께 찾는 것까지 보인다.
set SOURCE_ROOT_ERP=samples\colimpact
set SOURCE_ROOT_SAMPLE=samples\src

set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe

%PY% samples\demo\make_mail.py || goto :eof
%PY% app\app.py
