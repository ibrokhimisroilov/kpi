@echo off
chcp 65001 >nul
setlocal
rem ------------------------------------------------------------------
rem  Запуск бота на Windows: дважды щёлкните по этому файлу.
rem  Первый запуск создаёт папку .venv и скачивает библиотеки (несколько минут).
rem  Остановить бота: Ctrl+C или закрыть это окно.
rem ------------------------------------------------------------------
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Бот «Эффективность»

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo Создан файл настроек .env — сейчас он откроется в Блокноте.
    echo Впишите BOT_TOKEN, ADMIN_IDS и GEMINI_API_KEY, сохраните файл
    echo и снова запустите run.bat. Подробная инструкция — в README.md.
    start "" notepad ".env"
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Первый запуск: создаю окружение Python в папке .venv ...
    call :find_python
    if errorlevel 1 goto no_python
    call %%PYTHON%% -m venv .venv
    if errorlevel 1 goto venv_failed
)

echo Проверяю библиотеки из requirements.txt ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --retries 2 -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo Не удалось установить библиотеки ^(нет интернета?^). Пробую запустить с уже установленными.
)

echo.
echo Запускаю бота. Остановить — Ctrl+C или закрыть это окно.
echo.
".venv\Scripts\python.exe" -m bot
echo.
echo Бот остановлен.
pause
exit /b 0

:find_python
rem Нужен Python 3.12 или новее: сначала py-лаунчер, затем python из PATH.
for %%P in ("py -3.12" "py -3" "python") do (
    %%~P -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=%%~P"
        exit /b 0
    )
)
exit /b 1

:no_python
echo.
echo Не найден Python 3.12 или новее.
echo Скачайте его бесплатно: https://www.python.org/downloads/
echo При установке отметьте галочку «Add python.exe to PATH», затем снова запустите run.bat.
echo.
pause
exit /b 1

:venv_failed
echo.
echo Не удалось создать папку .venv. Удалите её, если она есть, и запустите run.bat ещё раз.
echo.
pause
exit /b 1
