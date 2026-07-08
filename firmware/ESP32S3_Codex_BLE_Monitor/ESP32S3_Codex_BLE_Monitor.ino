#include <Arduino.h>
#include <ArduinoJson.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <SPI.h>
#include <Wire.h>

#define PANEL_4P2
#define DISPLAY_WIDTH 300
#define DISPLAY_HEIGHT 400
#define LANDSCAPE_WIDTH 400
#define LANDSCAPE_HEIGHT 300

#include <ST7305_4p2_BW_DisplayDriver.h>
#include <ST73xxPins.h>
#include <U8g2_for_ST73XX.h>

static const char *DEVICE_NAME = "ESP32S3-Codex";
static const char *SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e";
static const char *RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e";
static const char *TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e";

static const int PIN_LCD_DC = 5;
static const int PIN_LCD_RST = 41;
static const int PIN_LCD_CS = 40;
static const int PIN_LCD_SCLK = 11;
static const int PIN_LCD_SDIN = 12;
static const int PIN_I2C_SDA = 13;
static const int PIN_I2C_SCL = 14;
static const int PIN_BATTERY_ADC = 4;  // BAT_ADC, ESP32-S3 ADC1_CH3.
static const float BATTERY_ADC_RATIO = 3.0f;
static const uint8_t SHTC3_ADDR = 0x70;
static const int KEY_PINS[] = {1, 2, 3, 18};
static const int KEY_PIN_COUNT = sizeof(KEY_PINS) / sizeof(KEY_PINS[0]);

static const ST73xxPins LCD_PINS{
    PIN_LCD_DC,
    PIN_LCD_CS,
    PIN_LCD_SCLK,
    PIN_LCD_SDIN,
    PIN_LCD_RST,
};

ST7305_4p2_BW_DisplayDriver display(LCD_PINS, SPI);

class LandscapeDisplay : public ST73XX_UI {
public:
  explicit LandscapeDisplay(ST7305_4p2_BW_DisplayDriver &physicalDisplay)
      : ST73XX_UI(LANDSCAPE_WIDTH, LANDSCAPE_HEIGHT), physical(physicalDisplay) {}

  void writePoint(uint x, uint y, bool enabled) override {
    if (x >= LANDSCAPE_WIDTH || y >= LANDSCAPE_HEIGHT) {
      return;
    }
    physical.writePoint(DISPLAY_WIDTH - 1 - y, x, enabled);
  }

  void writePoint(uint x, uint y, uint16_t color) override {
    writePoint(x, y, color != 0);
  }

private:
  ST7305_4p2_BW_DisplayDriver &physical;
};

LandscapeDisplay canvas(display);
U8G2_FOR_ST73XX u8g2;
BLECharacteristic *txCharacteristic = nullptr;

enum DogMood {
  DOG_ENERGETIC,
  DOG_NORMAL,
  DOG_TIRED,
  DOG_EXHAUSTED,
  DOG_DYING,
};

bool bleConnected = false;
String rxBuffer;
bool hasPcData = false;

struct WindowInfo {
  String label = "-";
  int used = -1;
  int remaining = -1;
  String reset = "-";
};

struct UsageInfo {
  bool ok = false;
  bool codexRunning = false;
  String status = "waiting";
  String plan = "-";
  String error = "";
  String date = "--";
  String time = "--:--:--";
  String updated = "--:--:--";
  WindowInfo primary;
  WindowInfo secondary;
};

UsageInfo usage;

struct StockInfo {
  String name = "-";
  String code = "-";
  float price = NAN;
  float pct = NAN;
  float change = NAN;
  uint8_t trend[32] = {50};
  int trendCount = 0;
};

StockInfo stocks[3];
int currentPage = 0;
bool lastKeyDown = false;
unsigned long lastKeyMs = 0;
float roomTempC = NAN;
float roomHumidity = NAN;
bool shtc3Ok = false;
unsigned long lastRoomSensorReadMs = 0;
float batteryVoltage = NAN;
int batteryPercent = -1;
unsigned long lastBatteryReadMs = 0;
unsigned long lastDogFrameMs = 0;
unsigned long lastDisplayDrawMs = 0;
unsigned long lastDisplayMaintenanceMs = 0;
int dogFrame = 0;
int dogX = 160;
int dogDirection = 1;
String currentFooter = "Starting BLE...";
String requestedFooter = "";
volatile bool redrawRequested = false;

static const unsigned long DOG_FRAME_INTERVAL_MS = 8000;
static const unsigned long DISPLAY_MAINTENANCE_MS = 300000;
static const unsigned long DISPLAY_HEARTBEAT_MS = 60000;

uint8_t crc8(const uint8_t *data, int len) {
  uint8_t crc = 0xFF;
  for (int i = 0; i < len; i++) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; bit++) {
      crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : (crc << 1);
    }
  }
  return crc;
}

void shtc3Command(uint16_t command) {
  Wire.beginTransmission(SHTC3_ADDR);
  Wire.write(command >> 8);
  Wire.write(command & 0xFF);
  Wire.endTransmission();
}

bool readShtc3(float &temperatureC, float &humidity) {
  shtc3Command(0x3517);  // Wake up.
  delay(2);
  shtc3Command(0x7CA2);  // Normal mode, clock stretching, temperature first.
  delay(15);

  if (Wire.requestFrom(SHTC3_ADDR, (uint8_t)6) != 6) {
    shtc3Command(0xB098);  // Sleep.
    return false;
  }

  uint8_t buf[6];
  for (int i = 0; i < 6; i++) {
    buf[i] = Wire.read();
  }
  shtc3Command(0xB098);  // Sleep.

  if (crc8(buf, 2) != buf[2] || crc8(buf + 3, 2) != buf[5]) {
    return false;
  }

  uint16_t rawT = (uint16_t(buf[0]) << 8) | buf[1];
  uint16_t rawRH = (uint16_t(buf[3]) << 8) | buf[4];
  temperatureC = -45.0f + 175.0f * float(rawT) / 65535.0f;
  humidity = 100.0f * float(rawRH) / 65535.0f;
  return true;
}

void updateRoomSensor() {
  if (millis() - lastRoomSensorReadMs < 10000 && !isnan(roomTempC)) {
    return;
  }
  lastRoomSensorReadMs = millis();

  float t = NAN;
  float h = NAN;
  shtc3Ok = readShtc3(t, h);
  if (shtc3Ok) {
    roomTempC = t;
    roomHumidity = h;
  }
}

int batteryPercentFromVoltage(float voltage) {
  if (isnan(voltage) || voltage <= 0.0f) {
    return -1;
  }
  if (voltage >= 4.20f) {
    return 100;
  }
  if (voltage <= 3.30f) {
    return 0;
  }

  struct Point {
    float voltage;
    int percent;
  };
  static const Point curve[] = {
      {4.20f, 100}, {4.10f, 90}, {4.00f, 80}, {3.92f, 70}, {3.85f, 60},
      {3.79f, 50},  {3.73f, 40}, {3.68f, 30}, {3.61f, 20}, {3.50f, 10},
      {3.30f, 0},
  };

  for (int i = 0; i < int(sizeof(curve) / sizeof(curve[0])) - 1; i++) {
    if (voltage <= curve[i].voltage && voltage >= curve[i + 1].voltage) {
      float span = curve[i].voltage - curve[i + 1].voltage;
      float ratio = span > 0 ? (voltage - curve[i + 1].voltage) / span : 0;
      return curve[i + 1].percent + round(ratio * (curve[i].percent - curve[i + 1].percent));
    }
  }
  return -1;
}

void updateBattery() {
  if (PIN_BATTERY_ADC < 0) {
    batteryVoltage = NAN;
    batteryPercent = -1;
    return;
  }

  if (millis() - lastBatteryReadMs < 5000 && !isnan(batteryVoltage)) {
    return;
  }
  lastBatteryReadMs = millis();

  uint32_t mv = analogReadMilliVolts(PIN_BATTERY_ADC);
  batteryVoltage = (float)mv * BATTERY_ADC_RATIO / 1000.0f;
  batteryPercent = batteryPercentFromVoltage(batteryVoltage);
}

void drawText(int x, int y, const char *text, const uint8_t *font, uint16_t fg = ST7305_COLOR_BLACK,
              uint16_t bg = ST7305_COLOR_WHITE) {
  u8g2.setFont(font);
  u8g2.setForegroundColor(fg);
  u8g2.setBackgroundColor(bg);
  u8g2.drawUTF8(x, y, text);
}

void wakeDisplay() {
  display.High_Power_Mode();
  display.display_on(true);
  display.display_Inversion(false);
}

void requestRedraw(const char *footer) {
  requestedFooter = footer;
  redrawRequested = true;
}

void drawBatteryStatus(int x, int y, bool inverted = false) {
  updateBattery();
  uint16_t fg = inverted ? ST7305_COLOR_WHITE : ST7305_COLOR_BLACK;
  uint16_t bg = inverted ? ST7305_COLOR_BLACK : ST7305_COLOR_WHITE;
  char line[32];
  if (batteryPercent >= 0) {
    snprintf(line, sizeof(line), "BAT %d%%", batteryPercent);
  } else {
    snprintf(line, sizeof(line), "BAT --");
  }
  drawText(x, y, line, u8g2_font_7x14_tf, fg, bg);

  int bx = x + 58;
  int by = y - 11;
  int bw = 24;
  int bh = 10;
  canvas.drawRectangle(bx, by, bx + bw, by + bh, fg);
  canvas.drawFilledRectangle(bx + bw + 1, by + 3, bx + bw + 3, by + 7, fg);
  if (batteryPercent >= 0) {
    int fillW = max(0, min(bw - 4, (bw - 4) * batteryPercent / 100));
    if (fillW > 0) {
      canvas.drawFilledRectangle(bx + 2, by + 2, bx + 2 + fillW, by + bh - 2, fg);
    }
  } else {
    canvas.drawLine(bx + 4, by + 2, bx + bw - 4, by + bh - 2, fg);
  }
}

DogMood currentDogMood() {
  int used = usage.primary.used;
  if (used < 0) {
    return DOG_NORMAL;
  }
  if (used < 20) {
    return DOG_ENERGETIC;
  }
  if (used < 45) {
    return DOG_NORMAL;
  }
  if (used < 70) {
    return DOG_TIRED;
  }
  if (used < 90) {
    return DOG_EXHAUSTED;
  }
  return DOG_DYING;
}

const char *dogMoodLabel(DogMood mood) {
  switch (mood) {
    case DOG_ENERGETIC:
      return "energetic";
    case DOG_NORMAL:
      return "normal";
    case DOG_TIRED:
      return "tired";
    case DOG_EXHAUSTED:
      return "exhausted";
    case DOG_DYING:
      return "dying";
  }
  return "normal";
}

void dogLine(int cx, int cy, int dir, int x1, int y1, int x2, int y2) {
  int ax = cx + dir * x1;
  int bx = cx + dir * x2;
  int ay = cy + y1;
  int by = cy + y2;
  canvas.drawLine(ax, ay, bx, by, ST7305_COLOR_BLACK);
  canvas.drawLine(ax, ay + 1, bx, by + 1, ST7305_COLOR_BLACK);
  canvas.drawLine(ax, ay - 1, bx, by - 1, ST7305_COLOR_BLACK);
}

void dogCircle(int cx, int cy, int dir, int x, int y, int r, bool filled = false) {
  if (filled) {
    canvas.drawFilledCircle(cx + dir * x, cy + y, r, ST7305_COLOR_BLACK);
  } else {
    int px = cx + dir * x;
    int py = cy + y;
    canvas.drawCircle(px, py, r, ST7305_COLOR_BLACK);
    canvas.drawCircle(px, py, max(1, r - 1), ST7305_COLOR_BLACK);
    canvas.drawCircle(px, py, r + 1, ST7305_COLOR_BLACK);
  }
}

void dogEllipse(int cx, int cy, int rx, int ry) {
  const int steps = 20;
  int prevX = cx + rx;
  int prevY = cy;
  for (int i = 1; i <= steps; i++) {
    float a = TWO_PI * i / steps;
    int x = cx + round(cos(a) * rx);
    int y = cy + round(sin(a) * ry);
    canvas.drawLine(prevX, prevY, x, y, ST7305_COLOR_BLACK);
    canvas.drawLine(prevX, prevY + 1, x, y + 1, ST7305_COLOR_BLACK);
    canvas.drawLine(prevX, prevY - 1, x, y - 1, ST7305_COLOR_BLACK);
    prevX = x;
    prevY = y;
  }
}

void dogCurve(int cx, int cy, int dir, int x0, int y0, int cpx, int cpy, int x1, int y1) {
  const int steps = 8;
  int prevX = x0;
  int prevY = y0;
  for (int i = 1; i <= steps; i++) {
    float t = float(i) / steps;
    float mt = 1.0f - t;
    int x = round(mt * mt * x0 + 2.0f * mt * t * cpx + t * t * x1);
    int y = round(mt * mt * y0 + 2.0f * mt * t * cpy + t * t * y1);
    dogLine(cx, cy, dir, prevX, prevY, x, y);
    prevX = x;
    prevY = y;
  }
}

void drawDogFace(int cx, int cy, int dir, DogMood mood, bool blink) {
  if (mood == DOG_DYING) {
    dogLine(cx, cy, dir, -9, -5, -2, 2);
    dogLine(cx, cy, dir, -2, -5, -9, 2);
    dogLine(cx, cy, dir, 14, -5, 21, 2);
    dogLine(cx, cy, dir, 21, -5, 14, 2);
  } else if (blink || mood == DOG_TIRED) {
    dogLine(cx, cy, dir, -10, -4, -2, -4);
    dogLine(cx, cy, dir, 14, -4, 22, -4);
  } else {
    dogCircle(cx, cy, dir, -6, -5, 3, true);
    dogCircle(cx, cy, dir, 18, -5, 3, true);
  }

  dogCircle(cx, cy, dir, 6, 1, 4, true);
  dogLine(cx, cy, dir, 6, 5, 1, 10);
  dogLine(cx, cy, dir, 6, 5, 14, 10);
  if (mood == DOG_ENERGETIC || mood == DOG_NORMAL) {
    dogCurve(cx, cy, dir, 5, 11, 9, 19, 14, 11);
  }
}

void drawMoodDog(int cx, int cy) {
  DogMood mood = currentDogMood();
  int dir = dogDirection >= 0 ? 1 : -1;
  int walk = dogFrame % 4;
  int bob = (walk == 1 || walk == 3) ? -2 : 0;
  int tail = (walk % 2 == 0) ? 8 : -8;
  bool blink = (dogFrame % 7) == 0;

  if (mood == DOG_DYING) {
    cy += 28;
    dogCurve(cx, cy, dir, -70, 5, -52, -18, -20, -14);
    dogCurve(cx, cy, dir, -20, -14, 8, -27, 54, -14);
    dogCurve(cx, cy, dir, 54, -14, 78, -4, 68, 20);
    dogCurve(cx, cy, dir, 68, 20, 28, 34, -38, 26);
    dogCurve(cx, cy, dir, -38, 26, -76, 23, -70, 5);
    drawDogFace(cx + dir * 20, cy - 2, dir, mood, blink);
    dogLine(cx, cy, dir, -68, 2, -88, -8);
    drawText(cx - 10, cy - 34, "zzz", u8g2_font_6x12_tf);
    return;
  }

  if (mood == DOG_EXHAUSTED) {
    cy += 15;
    tail = 0;
  }

  if (mood == DOG_TIRED) {
    bob += 4;
    tail = 2;
  } else if (mood == DOG_ENERGETIC) {
    bob -= 5;
    tail *= 2;
  }

  cy += bob;

  dogCurve(cx, cy, dir, -76, -16, -89, -38, -58, -48);
  dogCurve(cx, cy, dir, -58, -48, -34, -53, -19, -37);
  dogCurve(cx, cy, dir, -19, -37, -6, -60, 32, -56);
  dogCurve(cx, cy, dir, 32, -56, 50, -57, 58, -38);
  dogCurve(cx, cy, dir, 58, -38, 82, -48, 91, -24);
  dogCurve(cx, cy, dir, 91, -24, 96, -4, 75, 5);
  dogCurve(cx, cy, dir, 75, 5, 82, 29, 51, 40);
  dogCurve(cx, cy, dir, 51, 40, 29, 49, 5, 35);
  dogCurve(cx, cy, dir, 5, 35, -15, 52, -40, 42);
  dogCurve(cx, cy, dir, -40, 42, -64, 38, -54, 13);
  dogCurve(cx, cy, dir, -54, 13, -86, 4, -76, -16);

  dogLine(cx, cy, dir, -36, -50, -27, -42);
  dogLine(cx, cy, dir, 41, -50, 33, -41);
  dogLine(cx, cy, dir, -72, 6, -84, 20);
  dogLine(cx, cy, dir, 74, 5, 90, 16);

  drawDogFace(cx, cy - 13, dir, mood, blink);

  if (mood == DOG_EXHAUSTED) {
    dogLine(cx, cy, dir, -26, 15, -10, 25);
    dogLine(cx, cy, dir, 32, 16, 47, 24);
    drawText(cx + dir * 68 - 10, cy - 28, "...", u8g2_font_6x12_tf);
  } else {
    int paw = (walk % 2) ? -7 : 0;
    if (mood == DOG_ENERGETIC) {
      paw -= 5;
    } else if (mood == DOG_TIRED) {
      paw = 0;
    }
    dogLine(cx, cy, dir, -29, 12, -38, 30 + paw);
    dogLine(cx, cy, dir, 34, 11, 47, 28 - paw);
  }

  dogCurve(cx, cy, dir, -76, 8, -103, -7 + tail, -91, -33 + tail);
  dogCurve(cx, cy, dir, -91, -33 + tail, -74, -20 + tail, -86, -7 + tail);

  if (mood == DOG_ENERGETIC) {
    dogLine(cx, cy, dir, -101, 30, -82, 30);
    dogLine(cx, cy, dir, -94, 40, -76, 40);
  }
}

void drawBar(int x, int y, int w, int h, int percent) {
  canvas.drawRectangle(x, y, x + w, y + h, ST7305_COLOR_BLACK);
  if (percent < 0) {
    canvas.drawLine(x, y, x + w, y + h, ST7305_COLOR_BLACK);
    canvas.drawLine(x + w, y, x, y + h, ST7305_COLOR_BLACK);
    return;
  }

  int fillW = max(0, min(w - 4, (w - 4) * percent / 100));
  if (fillW > 0) {
    canvas.drawFilledRectangle(x + 2, y + 2, x + 2 + fillW, y + h - 2, ST7305_COLOR_BLACK);
  }
}

void drawWindow(int x, int y, int w, const WindowInfo &window, const char *fallbackLabel) {
  char line[72];
  const char *label = window.label.length() ? window.label.c_str() : fallbackLabel;
  snprintf(line, sizeof(line), "%s remaining", label);
  drawText(x, y, line, u8g2_font_helvB12_tf);

  if (window.remaining >= 0) {
    snprintf(line, sizeof(line), "%d%%", window.remaining);
  } else {
    snprintf(line, sizeof(line), "--%%");
  }
  drawText(x, y + 38, line, u8g2_font_logisoso32_tn);

  drawBar(x, y + 52, w, 22, window.remaining);

  if (window.used >= 0) {
    snprintf(line, sizeof(line), "used %d%%  reset %s", window.used, window.reset.c_str());
  } else {
    snprintf(line, sizeof(line), "used --%%  reset -");
  }
  drawText(x, y + 96, line, u8g2_font_6x12_tf);
}

void drawScreen(const char *footer) {
  updateRoomSensor();
  display.clearDisplay();
  canvas.drawRectangle(0, 0, LANDSCAPE_WIDTH - 1, LANDSCAPE_HEIGHT - 1, ST7305_COLOR_BLACK);

  canvas.drawFilledRectangle(0, 0, LANDSCAPE_WIDTH - 1, 46, ST7305_COLOR_BLACK);
  drawText(16, 32, "CODEX BLE", u8g2_font_helvB18_tf, ST7305_COLOR_WHITE, ST7305_COLOR_BLACK);

  char line[96];
  snprintf(line, sizeof(line), "BLE: %s  Codex: %s", bleConnected ? "connected" : "waiting",
           usage.status.c_str());
  drawText(190, 22, line, u8g2_font_7x14_tf, ST7305_COLOR_WHITE, ST7305_COLOR_BLACK);

  snprintf(line, sizeof(line), "%s %s  Plan: %s", usage.date.c_str(), usage.time.c_str(), usage.plan.c_str());
  drawText(190, 39, line, u8g2_font_6x12_tf, ST7305_COLOR_WHITE, ST7305_COLOR_BLACK);

  if (shtc3Ok) {
    snprintf(line, sizeof(line), "Room %.1fC  %.0f%%RH", roomTempC, roomHumidity);
  } else {
    snprintf(line, sizeof(line), "Room sensor: --");
  }
  drawText(24, 70, line, u8g2_font_helvB12_tf);
  if (hasPcData) {
    drawMoodDog(dogX, 92);
    snprintf(line, sizeof(line), "pet: %s", dogMoodLabel(currentDogMood()));
    drawText(290, 76, line, u8g2_font_6x12_tf);
  }

  if (hasPcData) {
    drawWindow(24, 120, 160, usage.primary, "5h");
    canvas.drawLine(200, 88, 200, 258, ST7305_COLOR_BLACK);
    drawWindow(224, 120, 150, usage.secondary, "7d");
    if (!usage.ok && usage.error.length()) {
      drawText(24, 268, "Quota unavailable", u8g2_font_6x12_tf);
    }
  } else {
    drawText(24, 128, "Waiting for PC data", u8g2_font_helvB18_tf);
    drawText(24, 170, "Run codex_ble_sender.py", u8g2_font_7x14_tf);
    if (usage.error.length()) {
      drawText(24, 210, usage.error.c_str(), u8g2_font_7x14_tf);
    }
  }

  drawText(16, 286, footer, u8g2_font_6x12_tf);
  drawBatteryStatus(306, 286);
  wakeDisplay();
  display.display();
  lastDisplayDrawMs = millis();
}

void drawTrend(int x, int y, int w, int h, const StockInfo &stock) {
  canvas.drawLine(x, y + h - 1, x + w, y + h - 1, ST7305_COLOR_BLACK);
  if (stock.trendCount < 2) {
    canvas.drawLine(x, y + h / 2, x + w, y + h / 2, ST7305_COLOR_BLACK);
    return;
  }

  int prevX = x;
  int prevY = y + h - 1 - (stock.trend[0] * (h - 3) / 100);
  for (int i = 1; i < stock.trendCount; i++) {
    int px = x + (i * w / (stock.trendCount - 1));
    int py = y + h - 1 - (stock.trend[i] * (h - 3) / 100);
    canvas.drawLine(prevX, prevY, px, py, ST7305_COLOR_BLACK);
    prevX = px;
    prevY = py;
  }
}

void drawStockRow(int y, const StockInfo &stock) {
  char line[96];
  snprintf(line, sizeof(line), "%s", stock.name.c_str());
  drawText(14, y + 20, line, u8g2_font_wqy16_t_gb2312b);
  drawText(14, y + 36, stock.code.c_str(), u8g2_font_6x12_tf);

  if (isnan(stock.price)) {
    snprintf(line, sizeof(line), "--");
  } else {
    snprintf(line, sizeof(line), "%.2f", stock.price);
  }
  drawText(118, y + 22, line, u8g2_font_helvB14_tf);

  if (isnan(stock.pct)) {
    snprintf(line, sizeof(line), "--%%");
  } else {
    snprintf(line, sizeof(line), "%+.2f%%", stock.pct);
  }
  drawText(196, y + 22, line, u8g2_font_helvB14_tf);

  if (isnan(stock.change)) {
    snprintf(line, sizeof(line), "--");
  } else {
    snprintf(line, sizeof(line), "%+.2f", stock.change);
  }
  drawText(292, y + 22, line, u8g2_font_helvB14_tf);

  drawTrend(14, y + 44, 360, 30, stock);
  drawText(14, y + 88, "09:30", u8g2_font_6x12_tf);
  String endTime = usage.time.length() >= 5 ? usage.time.substring(0, 5) : String("--:--");
  drawText(344, y + 88, endTime.c_str(), u8g2_font_6x12_tf);
}

void drawStocksScreen(const char *footer) {
  display.clearDisplay();
  canvas.drawRectangle(0, 0, LANDSCAPE_WIDTH - 1, LANDSCAPE_HEIGHT - 1, ST7305_COLOR_BLACK);
  canvas.drawFilledRectangle(0, 0, LANDSCAPE_WIDTH - 1, 30, ST7305_COLOR_BLACK);
  drawText(14, 22, "STOCKS", u8g2_font_helvB18_tf, ST7305_COLOR_WHITE, ST7305_COLOR_BLACK);

  char line[64];
  snprintf(line, sizeof(line), "%s %s  10s sync", usage.date.c_str(), usage.time.c_str());
  drawText(190, 21, line, u8g2_font_7x14_tf, ST7305_COLOR_WHITE, ST7305_COLOR_BLACK);

  drawStockRow(34, stocks[0]);
  canvas.drawLine(8, 124, 392, 124, ST7305_COLOR_BLACK);
  drawStockRow(126, stocks[1]);
  canvas.drawLine(8, 216, 392, 216, ST7305_COLOR_BLACK);
  drawStockRow(218, stocks[2]);

  drawText(14, 294, footer, u8g2_font_6x12_tf);
  drawBatteryStatus(306, 294);
  wakeDisplay();
  display.display();
  lastDisplayDrawMs = millis();
}

void drawCurrentPage(const char *footer) {
  currentFooter = footer;
  if (currentPage == 0) {
    drawScreen(footer);
  } else {
    drawStocksScreen(footer);
  }
}

WindowInfo parseWindow(JsonVariantConst node, const char *fallbackLabel) {
  WindowInfo window;
  if (node.isNull()) {
    window.label = fallbackLabel;
    return window;
  }
  window.label = node["label"] | fallbackLabel;
  window.used = node["used"] | -1;
  window.remaining = node["remaining"] | -1;
  window.reset = node["reset"] | "-";
  return window;
}

void sendAck(const char *message) {
  if (!txCharacteristic || !bleConnected) {
    return;
  }
  txCharacteristic->setValue(message);
  txCharacteristic->notify();
}

void handleJsonLine(const String &line) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) {
    usage.ok = false;
    usage.error = String("JSON: ") + err.c_str();
    requestRedraw("Bad JSON from PC.");
    sendAck("ERR json");
    return;
  }

  usage.ok = doc["ok"] | false;
  usage.codexRunning = doc["codex_running"] | false;
  usage.status = doc["status"] | "-";
  usage.plan = doc["plan_type"] | "-";
  usage.date = doc["date"] | "--";
  usage.time = doc["time"] | "--:--:--";
  usage.updated = doc["updated"] | "--:--:--";
  usage.error = doc["error"] | "";
  usage.primary = parseWindow(doc["primary"], "5h");
  usage.secondary = parseWindow(doc["secondary"], "7d");
  hasPcData = true;

  JsonArrayConst stockArray = doc["stocks"].as<JsonArrayConst>();
  int idx = 0;
  for (JsonObjectConst item : stockArray) {
    if (idx >= 3) {
      break;
    }
    stocks[idx].name = item["n"] | "-";
    stocks[idx].code = item["c"] | "-";
    stocks[idx].price = item["p"] | NAN;
    stocks[idx].pct = item["z"] | NAN;
    stocks[idx].change = item["d"] | NAN;
    stocks[idx].trendCount = 0;
    JsonArrayConst trend = item["t"].as<JsonArrayConst>();
    for (JsonVariantConst point : trend) {
      if (stocks[idx].trendCount >= 32) {
        break;
      }
      stocks[idx].trend[stocks[idx].trendCount++] = point.as<int>();
    }
    idx++;
  }

  requestRedraw("Updated from PC via BLE.");
  sendAck("OK updated");
}

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *server) override {
    bleConnected = true;
    Serial.println("BLE client connected.");
    requestRedraw("BLE connected.");
  }

  void onDisconnect(BLEServer *server) override {
    bleConnected = false;
    Serial.println("BLE client disconnected. Advertising again.");
    server->getAdvertising()->start();
    requestRedraw("BLE disconnected.");
  }
};

class RxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *characteristic) override {
    String value = characteristic->getValue().c_str();
    if (value.length() == 0) {
      return;
    }

    rxBuffer += value;
    int newline = rxBuffer.indexOf('\n');
    while (newline >= 0) {
      String line = rxBuffer.substring(0, newline);
      rxBuffer.remove(0, newline + 1);
      line.trim();
      if (line.length()) {
        Serial.print("BLE JSON: ");
        Serial.println(line);
        handleJsonLine(line);
      }
      newline = rxBuffer.indexOf('\n');
    }
  }
};

void setupBle() {
  BLEDevice::init(DEVICE_NAME);
  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());

  BLEService *service = server->createService(SERVICE_UUID);

  txCharacteristic = service->createCharacteristic(
      TX_UUID,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  txCharacteristic->addDescriptor(new BLE2902());
  txCharacteristic->setValue("ESP32-S3 Codex BLE ready.");

  BLECharacteristic *rxCharacteristic = service->createCharacteristic(
      RX_UUID,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  rxCharacteristic->setCallbacks(new RxCallbacks());

  service->start();

  BLEAdvertisementData advertisementData;
  advertisementData.setFlags(0x06);
  advertisementData.setCompleteServices(BLEUUID(SERVICE_UUID));

  BLEAdvertisementData scanResponseData;
  scanResponseData.setName(DEVICE_NAME);

  BLEAdvertising *advertising = server->getAdvertising();
  advertising->setAdvertisementData(advertisementData);
  advertising->setScanResponseData(scanResponseData);
  advertising->setMinPreferred(0x06);
  advertising->setMaxPreferred(0x12);
  advertising->start();

  Serial.print("BLE advertising as: ");
  Serial.println(DEVICE_NAME);
}

void setup() {
  Serial.begin(115200);
  delay(500);

  for (int i = 0; i < KEY_PIN_COUNT; i++) {
    pinMode(KEY_PINS[i], INPUT_PULLUP);
  }

  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
  Wire.setClock(100000);
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_BATTERY_ADC, ADC_11db);

  SPI.begin(PIN_LCD_SCLK, -1, PIN_LCD_SDIN, PIN_LCD_CS);
  display.initialize();
  display.High_Power_Mode();
  display.display_on(true);
  display.display_Inversion(false);
  u8g2.begin(canvas);
  u8g2.setFontMode(1);

  drawCurrentPage("Starting BLE...");
  setupBle();
  drawCurrentPage("Advertising ESP32S3-Codex.");
}

void loop() {
  bool keyDown = false;
  for (int i = 0; i < KEY_PIN_COUNT; i++) {
    if (digitalRead(KEY_PINS[i]) == LOW) {
      keyDown = true;
      break;
    }
  }

  if (keyDown && !lastKeyDown && millis() - lastKeyMs > 250) {
    lastKeyMs = millis();
    currentPage = currentPage == 0 ? 1 : 0;
    requestRedraw(currentPage == 0 ? "Page: Codex" : "Page: Stocks");
  }
  lastKeyDown = keyDown;

  if (redrawRequested) {
    redrawRequested = false;
    drawCurrentPage(requestedFooter.length() ? requestedFooter.c_str() : currentFooter.c_str());
  }

  if (currentPage == 0 && hasPcData && millis() - lastDogFrameMs > DOG_FRAME_INTERVAL_MS) {
    lastDogFrameMs = millis();
    dogFrame++;

    DogMood mood = currentDogMood();
    int step = 0;
    switch (mood) {
      case DOG_ENERGETIC:
        step = 16;
        break;
      case DOG_NORMAL:
        step = 9;
        break;
      case DOG_TIRED:
        step = 4;
        break;
      case DOG_EXHAUSTED:
        step = 2;
        break;
      case DOG_DYING:
        step = 0;
        break;
    }

    dogX += dogDirection * step;
    if (dogX > 280) {
      dogX = 280;
      dogDirection = -1;
    } else if (dogX < 126) {
      dogX = 126;
      dogDirection = 1;
    }
    requestRedraw(currentFooter.c_str());
  }

  if (millis() - lastDisplayMaintenanceMs > DISPLAY_MAINTENANCE_MS) {
    lastDisplayMaintenanceMs = millis();
    wakeDisplay();
    requestRedraw(currentFooter.c_str());
  } else if (millis() - lastDisplayDrawMs > DISPLAY_HEARTBEAT_MS) {
    requestRedraw(currentFooter.c_str());
  }
  delay(100);
}
