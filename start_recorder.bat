@echo off
rem Автозапуск рекордера при входе оператора на терминальный сервер.
rem Положить ярлык на этот файл в общую папку автозагрузки:
rem   C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp
rem Пути ниже поправьте под свой сервер.

set RECORDER_HOME=C:\ActionRecorder
set RECORDER_DIR=D:\Recordings

rem Записывать только выбранных сотрудников: логины через пробел, каждый с пробелом с обеих сторон.
rem Пусто - записываются все, кто входит на сервер.
set RECORD_USERS=

if not "%RECORD_USERS%"=="" (
  echo " %RECORD_USERS% " | findstr /i /c:" %USERNAME% " >nul || exit /b 0
)

if exist "%RECORDER_HOME%\recorder.exe" (
  start "" "%RECORDER_HOME%\recorder.exe" --base "%RECORDER_DIR%"
) else (
  start "" "%RECORDER_HOME%\.venv\Scripts\pythonw.exe" "%RECORDER_HOME%\recorder.py" --base "%RECORDER_DIR%"
)
