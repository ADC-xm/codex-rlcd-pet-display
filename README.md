# Codex RLCD Pet Display

一个运行在 Waveshare/微雪 `ESP32-S3-RLCD-4.2` 黑白全反射屏上的 Codex 额度桌面小屏。

它通过电脑端 Python 脚本读取本机 Codex 账号的 rate limit，再用 BLE 同步到 ESP32-S3。主屏显示 Codex 额度、时间、房间温湿度、电池电量，并带一个会随 5h 额度使用情况变化心情的线条桌宠。

## Features

- ESP32-S3 + 4.2 inch RLCD 横屏 UI，适配 `300x400` ST7305 黑白屏
- BLE 串口式同步，不依赖 USB 数据线传输
- Codex 5h / 7d quota remaining 显示
- 桌宠 mood 跟随 5h `used%` 自动变化
  - `<20% used`: energetic
  - `<45% used`: normal
  - `<70% used`: tired
  - `<90% used`: exhausted
  - `>=90% used`: dying
- SHTC3 室温/湿度显示
- 18650 电池电量显示，`BAT_ADC = GPIO4`
- 可选股票页，支持 A 股行情和分时线同步
- Windows 开机自启动桥接脚本

## Hardware

Tested with:

- Board: `ESP32-S3-RLCD-4.2`
- MCU: `ESP32-S3-WROOM-1-N16R8`
- Display: 4.2 inch reflective LCD, `300x400`, ST7305
- Sensor: SHTC3 on I2C
- Battery ADC: `GPIO4`, resistor divider ratio `3.0`

Important pins used by the firmware:

```text
RLCD_DC     GPIO5
RLCD_TE     GPIO6
RLCD_SCK    GPIO11
RLCD_DIN    GPIO12
RLCD_CS     GPIO40
RLCD_RESET  GPIO41
I2C_SDA     GPIO13
I2C_SCL     GPIO14
BAT_ADC     GPIO4
```

## Project Layout

```text
firmware/
  ESP32S3_Codex_BLE_Monitor/
    ESP32S3_Codex_BLE_Monitor.ino

bridge/
  codex_ble_sender.py
  codex_ble_autostart_watch.py
  Start-Codex-BLE-Autostart-Watch.bat
  Start-Codex-BLE-Sender-Visible.bat

previews/
  preview_codex_fluffy_dog_v3.html
  preview_codex_mood_dog_v2.html
  preview_codex_line_dog.html
```

## Setup

### 1. Arduino

Install:

- Arduino IDE 2.x
- ESP32 core `3.3.x`
- `ArduinoJson`
- `ST7305_MonoTFT_Library`

Recommended board config:

```text
FQBN:
esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB
```

Open and upload:

```text
firmware/ESP32S3_Codex_BLE_Monitor/ESP32S3_Codex_BLE_Monitor.ino
```

After flashing, the board advertises BLE as:

```text
ESP32S3-Codex
```

### 2. Python Bridge

Install Python dependencies:

```powershell
pip install -r requirements.txt
```

Run the visible sender for debugging:

```powershell
bridge\Start-Codex-BLE-Sender-Visible.bat
```

Run the background watcher:

```powershell
bridge\Start-Codex-BLE-Autostart-Watch.bat
```

The watcher keeps the BLE sender alive. On Windows you can put the `.bat` in the Startup folder to start syncing after login.

## How It Works

1. `codex_ble_sender.py` starts `codex app-server --listen stdio://`.
2. It calls `account/rateLimits/read`.
3. It builds a compact JSON payload containing quota, time, and optional stock data.
4. It sends the JSON over BLE in chunks to the ESP32-S3.
5. The firmware redraws the RLCD home page and updates the mood pet.

Example payload shape:

```json
{
  "ok": true,
  "status": "running",
  "date": "07-08",
  "time": "11:03:44",
  "primary": {"label": "5h", "used": 43, "remaining": 57, "reset": "07-08 14:30"},
  "secondary": {"label": "7d", "used": 32, "remaining": 68, "reset": "07-14 00:00"}
}
```

## Notes

- This is a hobby desktop display, not an official OpenAI product.
- The BLE link is local between your PC and ESP32-S3.
- The bridge reads Codex quota from the local Codex CLI/app-server. Your prompt text is not sent to the board.
- Stock data uses public market endpoints and is best-effort.

## License

MIT
