@echo off
cd /d "%~dp0"
echo Starting visible Codex BLE sender...
echo.
echo This window shows scanning, connection, and update logs.
echo Close this window to stop this visible sender.
echo.
C:\Python313\python.exe -u codex_ble_sender.py
pause
