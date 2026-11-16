#include <Arduino.h>
#include <WebServer.h>
#include <WiFi.h>
#include <cstring>
#include "esp_camera.h"
#include "esp_err.h"
#include "timercamera_f_pins.h"

#if __has_include("local_config.h")
#include "local_config.h"
#endif

#ifndef VRC_WIFI_SSID
#define VRC_WIFI_SSID ""
#endif

#ifndef VRC_WIFI_PASSWORD
#define VRC_WIFI_PASSWORD ""
#endif

#ifndef VRC_DEVICE_NAME
#define VRC_DEVICE_NAME "m5stack_timer_camera_f"
#endif

#ifndef VRC_STREAM_BOUNDARY
#define VRC_STREAM_BOUNDARY "frame"
#endif

#ifndef VRC_CAMERA_FRAME_SIZE
#define VRC_CAMERA_FRAME_SIZE FRAMESIZE_VGA
#endif

#ifndef VRC_CAMERA_JPEG_QUALITY
#define VRC_CAMERA_JPEG_QUALITY 12
#endif

#ifndef VRC_STREAM_FRAME_DELAY_MS
#define VRC_STREAM_FRAME_DELAY_MS 100
#endif

namespace {

WebServer server(80);

bool cameraReady = false;
esp_err_t lastCameraError = ESP_OK;
framesize_t currentFrameSize = VRC_CAMERA_FRAME_SIZE;
int currentJpegQuality = VRC_CAMERA_JPEG_QUALITY;
uint32_t currentStreamDelayMs = VRC_STREAM_FRAME_DELAY_MS;
uint32_t totalFrames = 0;
uint32_t lastFrameBytes = 0;
uint32_t activeStreamClients = 0;
uint32_t lastReconnectAttemptMs = 0;
bool httpServerStarted = false;

constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 20000;
constexpr uint32_t WIFI_RECONNECT_INTERVAL_MS = 10000;

const char *wifiSsid = VRC_WIFI_SSID;
const char *wifiPassword = VRC_WIFI_PASSWORD;

struct FrameSizeOption {
    const char *name;
    framesize_t value;
    uint16_t width;
    uint16_t height;
};

constexpr FrameSizeOption FRAME_SIZE_OPTIONS[] = {
    {"QQVGA", FRAMESIZE_QQVGA, 160, 120},
    {"QCIF", FRAMESIZE_QCIF, 176, 144},
    {"HQVGA", FRAMESIZE_HQVGA, 240, 176},
    {"QVGA", FRAMESIZE_QVGA, 320, 240},
    {"CIF", FRAMESIZE_CIF, 400, 296},
    {"HVGA", FRAMESIZE_HVGA, 480, 320},
    {"VGA", FRAMESIZE_VGA, 640, 480},
    {"SVGA", FRAMESIZE_SVGA, 800, 600},
    {"XGA", FRAMESIZE_XGA, 1024, 768},
    {"HD", FRAMESIZE_HD, 1280, 720},
    {"SXGA", FRAMESIZE_SXGA, 1280, 1024},
    {"UXGA", FRAMESIZE_UXGA, 1600, 1200},
};

bool wifiCredentialsConfigured() {
    return std::strlen(wifiSsid) > 0;
}

int clampInt(int value, int minValue, int maxValue) {
    if (value < minValue) {
        return minValue;
    }
    if (value > maxValue) {
        return maxValue;
    }
    return value;
}

const FrameSizeOption *findFrameSize(framesize_t frameSize) {
    for (const FrameSizeOption &option : FRAME_SIZE_OPTIONS) {
        if (option.value == frameSize) {
            return &option;
        }
    }
    return nullptr;
}

const FrameSizeOption *findFrameSize(const String &requested) {
    String normalized = requested;
    normalized.trim();
    normalized.toUpperCase();

    for (const FrameSizeOption &option : FRAME_SIZE_OPTIONS) {
        if (normalized == option.name) {
            return &option;
        }

        String dimensions = String(option.width) + "X" + String(option.height);
        if (normalized == dimensions) {
            return &option;
        }
    }
    return nullptr;
}

bool parseBoundedIntArg(const char *name, int minValue, int maxValue, int &outValue) {
    String raw = server.arg(name);
    raw.trim();
    if (raw.length() == 0) {
        return false;
    }

    for (size_t i = 0; i < raw.length(); ++i) {
        const char c = raw.charAt(i);
        if (c < '0' || c > '9') {
            return false;
        }
    }

    const int value = raw.toInt();
    if (value < minValue || value > maxValue) {
        return false;
    }

    outValue = value;
    return true;
}

void setStatusLed(bool on) {
    digitalWrite(TIMER_CAMERA_F_LED_PIN, on ? HIGH : LOW);
}

void setupPowerHold() {
    pinMode(TIMER_CAMERA_F_BAT_HOLD_PIN, OUTPUT);
    digitalWrite(TIMER_CAMERA_F_BAT_HOLD_PIN, HIGH);
}

void setupStatusLed() {
    pinMode(TIMER_CAMERA_F_LED_PIN, OUTPUT);
    setStatusLed(false);
}

void updateStatusLed() {
    if (WiFi.status() == WL_CONNECTED) {
        setStatusLed(true);
        return;
    }

    setStatusLed((millis() / 250) % 2 == 0);
}

bool initializeCamera() {
    const bool hasPsram = psramFound();
    currentFrameSize = hasPsram ? VRC_CAMERA_FRAME_SIZE : FRAMESIZE_QVGA;
    currentJpegQuality = clampInt(VRC_CAMERA_JPEG_QUALITY, 4, 63);

    camera_config_t config = {};
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer = LEDC_TIMER_0;
    config.pin_d0 = TIMER_CAMERA_F_D0_PIN;
    config.pin_d1 = TIMER_CAMERA_F_D1_PIN;
    config.pin_d2 = TIMER_CAMERA_F_D2_PIN;
    config.pin_d3 = TIMER_CAMERA_F_D3_PIN;
    config.pin_d4 = TIMER_CAMERA_F_D4_PIN;
    config.pin_d5 = TIMER_CAMERA_F_D5_PIN;
    config.pin_d6 = TIMER_CAMERA_F_D6_PIN;
    config.pin_d7 = TIMER_CAMERA_F_D7_PIN;
    config.pin_xclk = TIMER_CAMERA_F_XCLK_PIN;
    config.pin_pclk = TIMER_CAMERA_F_PCLK_PIN;
    config.pin_vsync = TIMER_CAMERA_F_VSYNC_PIN;
    config.pin_href = TIMER_CAMERA_F_HREF_PIN;
    config.pin_sccb_sda = TIMER_CAMERA_F_SIOD_PIN;
    config.pin_sccb_scl = TIMER_CAMERA_F_SIOC_PIN;
    config.pin_pwdn = TIMER_CAMERA_F_PWDN_PIN;
    config.pin_reset = TIMER_CAMERA_F_RESET_PIN;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;
    config.frame_size = currentFrameSize;
    config.jpeg_quality = currentJpegQuality;
    config.fb_count = hasPsram ? 2 : 1;
    config.fb_location = hasPsram ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
    config.grab_mode = hasPsram ? CAMERA_GRAB_LATEST : CAMERA_GRAB_WHEN_EMPTY;

    lastCameraError = esp_camera_init(&config);
    if (lastCameraError != ESP_OK) {
        Serial.printf("Camera init failed: %s (0x%04x)\n", esp_err_to_name(lastCameraError), lastCameraError);
        return false;
    }

    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor != nullptr) {
        sensor->set_quality(sensor, currentJpegQuality);
        sensor->set_framesize(sensor, currentFrameSize);
    }

    Serial.printf("Camera initialized. PSRAM: %s\n", psramFound() ? "yes" : "no");
    return true;
}

bool applyCameraSettings(framesize_t frameSize, int jpegQuality) {
    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor == nullptr) {
        return false;
    }

    if (sensor->set_framesize(sensor, frameSize) != 0) {
        return false;
    }
    if (sensor->set_quality(sensor, jpegQuality) != 0) {
        return false;
    }

    currentFrameSize = frameSize;
    currentJpegQuality = jpegQuality;
    return true;
}

void connectWifi() {
    if (!wifiCredentialsConfigured()) {
        Serial.println("WiFi credentials are not configured.");
        return;
    }

    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.begin(wifiSsid, wifiPassword);

    Serial.printf("Connecting to WiFi SSID: %s", wifiSsid);
    const uint32_t startedMs = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - startedMs < WIFI_CONNECT_TIMEOUT_MS) {
        updateStatusLed();
        Serial.print(".");
        delay(250);
    }
    Serial.println();

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("WiFi connected. IP: %s\n", WiFi.localIP().toString().c_str());
    } else {
        Serial.println("WiFi connection timed out.");
    }
}

String jsonString(const String &value) {
    String escaped;
    escaped.reserve(value.length() + 2);
    escaped += '"';
    for (size_t i = 0; i < value.length(); ++i) {
        const char c = value[i];
        if (c == '"' || c == '\\') {
            escaped += '\\';
            escaped += c;
        } else if (c == '\n') {
            escaped += "\\n";
        } else if (c == '\r') {
            escaped += "\\r";
        } else if (c == '\t') {
            escaped += "\\t";
        } else {
            escaped += c;
        }
    }
    escaped += '"';
    return escaped;
}

String buildStatusJson() {
    const bool connected = WiFi.status() == WL_CONNECTED;
    const int batteryRaw = analogRead(TIMER_CAMERA_F_BAT_ADC_PIN);
    const FrameSizeOption *frameSize = findFrameSize(currentFrameSize);

    String body;
    body.reserve(768);
    body += "{";
    body += "\"device\":";
    body += jsonString(VRC_DEVICE_NAME);
    body += ",\"camera_ready\":";
    body += cameraReady ? "true" : "false";
    body += ",\"psram_found\":";
    body += psramFound() ? "true" : "false";
    body += ",\"wifi_connected\":";
    body += connected ? "true" : "false";
    body += ",\"ip\":";
    body += jsonString(connected ? WiFi.localIP().toString() : String(""));
    body += ",\"rssi\":";
    body += connected ? String(WiFi.RSSI()) : String(0);
    body += ",\"uptime_ms\":";
    body += String(millis());
    body += ",\"free_heap\":";
    body += String(ESP.getFreeHeap());
    body += ",\"free_psram\":";
    body += String(ESP.getFreePsram());
    body += ",\"battery_adc_raw\":";
    body += String(batteryRaw);
    body += ",\"frame_size\":";
    body += jsonString(frameSize != nullptr ? String(frameSize->name) : String("unknown"));
    body += ",\"frame_width\":";
    body += String(frameSize != nullptr ? frameSize->width : 0);
    body += ",\"frame_height\":";
    body += String(frameSize != nullptr ? frameSize->height : 0);
    body += ",\"jpeg_quality\":";
    body += String(currentJpegQuality);
    body += ",\"stream_delay_ms\":";
    body += String(currentStreamDelayMs);
    body += ",\"frames_sent\":";
    body += String(totalFrames);
    body += ",\"last_frame_bytes\":";
    body += String(lastFrameBytes);
    body += ",\"active_stream_clients\":";
    body += String(activeStreamClients);
    body += ",\"last_camera_error\":";
    body += String(static_cast<uint32_t>(lastCameraError));
    body += "}\n";
    return body;
}

void handleStatus() {
    server.sendHeader("Cache-Control", "no-store");
    server.send(200, "application/json", buildStatusJson());
}

void handleControl() {
    if (!cameraReady) {
        server.send(503, "text/plain", "Camera is not initialized.\n");
        return;
    }

    framesize_t requestedFrameSize = currentFrameSize;
    int requestedJpegQuality = currentJpegQuality;
    int requestedStreamDelayMs = static_cast<int>(currentStreamDelayMs);

    if (server.hasArg("framesize")) {
        const FrameSizeOption *option = findFrameSize(server.arg("framesize"));
        if (option == nullptr) {
            server.send(400, "text/plain", "Invalid framesize.\n");
            return;
        }
        requestedFrameSize = option->value;
    }

    if (server.hasArg("quality") && !parseBoundedIntArg("quality", 4, 63, requestedJpegQuality)) {
        server.send(400, "text/plain", "Invalid quality. Use 4 to 63.\n");
        return;
    }

    if (server.hasArg("delay_ms") && !parseBoundedIntArg("delay_ms", 0, 5000, requestedStreamDelayMs)) {
        server.send(400, "text/plain", "Invalid delay_ms. Use 0 to 5000.\n");
        return;
    }

    if (!applyCameraSettings(requestedFrameSize, requestedJpegQuality)) {
        server.send(500, "text/plain", "Camera setting update failed.\n");
        return;
    }

    currentStreamDelayMs = static_cast<uint32_t>(requestedStreamDelayMs);
    server.sendHeader("Cache-Control", "no-store");
    server.send(200, "application/json", buildStatusJson());
}

camera_fb_t *captureFrameOrSendError() {
    if (!cameraReady) {
        server.send(503, "text/plain", "Camera is not initialized.\n");
        return nullptr;
    }

    camera_fb_t *frame = esp_camera_fb_get();
    if (frame == nullptr) {
        server.send(503, "text/plain", "Camera capture failed.\n");
        return nullptr;
    }

    if (frame->format != PIXFORMAT_JPEG) {
        esp_camera_fb_return(frame);
        server.send(500, "text/plain", "Camera frame is not JPEG.\n");
        return nullptr;
    }

    return frame;
}

void handleCapture() {
    camera_fb_t *frame = captureFrameOrSendError();
    if (frame == nullptr) {
        return;
    }

    lastFrameBytes = frame->len;
    ++totalFrames;

    server.sendHeader("Content-Disposition", "inline; filename=capture.jpg");
    server.sendHeader("Cache-Control", "no-store");
    server.setContentLength(frame->len);
    server.send(200, "image/jpeg", "");
    server.client().write(frame->buf, frame->len);

    esp_camera_fb_return(frame);
}

void handleStream() {
    if (!cameraReady) {
        server.send(503, "text/plain", "Camera is not initialized.\n");
        return;
    }

    WiFiClient client = server.client();
    client.println("HTTP/1.1 200 OK");
    client.println("Content-Type: multipart/x-mixed-replace; boundary=" VRC_STREAM_BOUNDARY);
    client.println("Cache-Control: no-store");
    client.println("Connection: close");
    client.println();

    ++activeStreamClients;
    while (client.connected()) {
        camera_fb_t *frame = esp_camera_fb_get();
        if (frame == nullptr) {
            client.println("--" VRC_STREAM_BOUNDARY);
            client.println("Content-Type: text/plain");
            client.println("Content-Length: 22");
            client.println();
            client.println("Camera capture failed");
            delay(currentStreamDelayMs);
            continue;
        }

        if (frame->format == PIXFORMAT_JPEG) {
            lastFrameBytes = frame->len;
            ++totalFrames;

            client.println("--" VRC_STREAM_BOUNDARY);
            client.println("Content-Type: image/jpeg");
            client.printf("Content-Length: %u\r\n", static_cast<unsigned int>(frame->len));
            client.println();
            client.write(frame->buf, frame->len);
            client.println();
        }

        esp_camera_fb_return(frame);
        delay(currentStreamDelayMs);
    }
    --activeStreamClients;
}

void handleNotFound() {
    server.send(404, "text/plain", "Not found.\n");
}

void startHttpServer() {
    if (httpServerStarted) {
        return;
    }

    server.on("/status", HTTP_GET, handleStatus);
    server.on("/control", HTTP_GET, handleControl);
    server.on("/capture.jpg", HTTP_GET, handleCapture);
    server.on("/stream", HTTP_GET, handleStream);
    server.onNotFound(handleNotFound);
    server.begin();
    httpServerStarted = true;
    Serial.println("HTTP server started on port 80.");
}

void startHttpServerIfReady() {
    if (WiFi.status() != WL_CONNECTED) {
        return;
    }
    startHttpServer();
}

void maintainWifi() {
    if (!wifiCredentialsConfigured() || WiFi.status() == WL_CONNECTED) {
        return;
    }

    const uint32_t nowMs = millis();
    if (nowMs - lastReconnectAttemptMs < WIFI_RECONNECT_INTERVAL_MS) {
        return;
    }

    lastReconnectAttemptMs = nowMs;
    Serial.println("WiFi disconnected. Reconnecting...");
    WiFi.disconnect();
    WiFi.begin(wifiSsid, wifiPassword);
}

}  // namespace

void setup() {
    setupPowerHold();
    setupStatusLed();
    analogReadResolution(12);

    Serial.begin(115200);
    delay(500);
    Serial.println();
    Serial.println("VRC Facial Tracker TimerCamera-F firmware starting.");

    cameraReady = initializeCamera();
    connectWifi();
    startHttpServerIfReady();
}

void loop() {
    updateStatusLed();
    maintainWifi();

    if (WiFi.status() == WL_CONNECTED) {
        startHttpServerIfReady();
        server.handleClient();
    }

    delay(2);
}
