# Windows Autostart

To start the BLE bridge when Windows logs in, create a shortcut to:

```text
bridge\Start-Codex-BLE-Autostart-Watch.bat
```

and put the shortcut in:

```text
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

The batch file starts `codex_ble_autostart_watch.py` with `pythonw.exe`, so it runs in the background without opening a terminal window.

To debug connection issues, run:

```text
bridge\Start-Codex-BLE-Sender-Visible.bat
```
