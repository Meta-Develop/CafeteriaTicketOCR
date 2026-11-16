# TimerCamera-F Firmware

PlatformIO Arduino firmware for the M5Stack TimerCamera-F.

## Hardware

Target board: M5Stack TimerCamera-F with ESP32, 8MB PSRAM, and OV3660 camera.

## WiFi Configuration

WiFi credentials must stay local. Choose one of these options:

1. Copy `include/local_config.example.h` to `include/local_config.h` and edit the values.
2. Define `VRC_WIFI_SSID` and `VRC_WIFI_PASSWORD` as build flags in a local PlatformIO environment.

`include/local_config.h` is ignored by git.

## Network boundary

Use the camera only on a trusted local network. The firmware exposes unauthenticated
HTTP endpoints, so do not port-forward the device or expose it directly to the
public Internet.

## Build

```bash
pio run -e m5stack_timer_camera_f
```

## Upload

```bash
pio run -e m5stack_timer_camera_f -t upload
```

## Endpoints

After the device connects to WiFi, open these URLs:

- `http://<device-ip>/stream` - MJPEG stream
- `http://<device-ip>/capture.jpg` - single JPEG capture
- `http://<device-ip>/status` - JSON status
- `http://<device-ip>/control` - camera control endpoint

Serial output uses 115200 baud and prints the assigned IP address.

## Control Parameters

The PC GUI sends these query parameters to `/control`:

- `framesize`: `QQVGA`, `QCIF`, `HQVGA`, `QVGA`, `CIF`, `HVGA`, `VGA`, `SVGA`, `XGA`, `HD`, `SXGA`, or `UXGA`
- `quality`: JPEG quality from `4` to `63`; lower values are higher quality
- `delay_ms`: MJPEG stream delay from `0` to `5000`

Example:

```text
http://<device-ip>/control?framesize=VGA&quality=12&delay_ms=100
```
