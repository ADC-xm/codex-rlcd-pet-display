# GitHub Repository Text

## Repository name

`codex-rlcd-pet-display`

## Short description

ESP32-S3 RLCD desktop display for Codex quota, room sensor data, battery status, and a mood pet synced over BLE.

## Chinese intro

一个基于 ESP32-S3-RLCD-4.2 的 Codex 额度桌面小屏。电脑端 Python 脚本读取本机 Codex rate limit，通过 BLE 同步到 ESP32-S3；板子首页显示 5h/7d 额度、时间、温湿度、电量，并根据 5h 已使用比例切换线条桌宠的心情状态。

## English intro

A tiny reflective LCD desktop companion for Codex users. A Python bridge reads local Codex rate-limit data and streams it to an ESP32-S3-RLCD-4.2 over BLE. The device shows 5h/7d quota remaining, time, room temperature/humidity, battery status, and a monochrome mood pet whose state follows Codex usage.

## Suggested topics

```text
esp32-s3
rlcd
codex
ble
arduino
desktop-widget
rate-limit
pet
st7305
waveshare
```

## First release notes

Initial open-source release.

- Codex 5h/7d quota home page
- BLE PC-to-ESP32 sync bridge
- SHTC3 room temperature and humidity
- 18650 battery percentage via BAT_ADC GPIO4
- Mood pet driven by Codex 5h used percentage
- Optional A-share stock quote page
