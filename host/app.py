from __future__ import annotations

import io
import json
import os
import queue
import re
import shutil
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

try:
    import cv2
    import numpy as np
    import pytesseract
    import serial
    from PIL import Image, ImageOps, ImageTk
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
except ImportError as exc:  # pragma: no cover - startup help
    missing = exc.name or "unknown"
    raise SystemExit(
        f"Missing required library: {missing}\n"
        "Run `python -m pip install -r host/requirements.txt` first."
    ) from exc

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ocr_menu import correct_ocr_text, select_best_menu_item


APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
MENU_DATABASE_PATH = REPO_ROOT / "data" / "menu.json"
UDP_MAGIC = b"XCV1"
UDP_PACKET_TYPE_FRAME = 1
UDP_HEADER = struct.Struct("<4sBBHIHHIHHHH")
UDP_STALE_FRAME_SECONDS = 1.5
VRC_CMD_REGISTER = b"\x01"
VRC_CMD_HEARTBEAT = b"\x02"
VRC_RECV_BUF_SIZE = 512 * 1024
TCP_STILL_DEFAULT_PORT = 5556
TCP_STILL_MAX_BYTES = 5 * 1024 * 1024
USB_STILL_DEFAULT_PORT = "COM6"
USB_STILL_DEFAULT_BAUD = 2_000_000
USB_STILL_MAX_BYTES = 5 * 1024 * 1024
CAPTURE_DIR = REPO_ROOT / "runtime" / "captures"
TESSDATA_DIR = REPO_ROOT / "runtime" / "tessdata"
TESSERACT_PATHS = [
    Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
]
DEFAULT_OCR_LANGUAGE = "jpn+eng"
DEFAULT_OCR_ENGINE = "rapidocr"
OCR_ENGINES = ("rapidocr", "tesseract")
OCR_FAST_PSM_MODES = (6, 11)
OCR_ACCURATE_PSM_MODES = (6, 11, 12, 3, 7)
OCR_FAST_LONG_EDGE = 1800
OCR_ACCURATE_LONG_EDGE = 2600
_RAPIDOCR_ENGINE: RapidOCR | None = None

TIMER_CAMERA_FRAME_SIZES = [
    ("QQVGA 160x120", "QQVGA"),
    ("QCIF 176x144", "QCIF"),
    ("HQVGA 240x176", "HQVGA"),
    ("QVGA 320x240", "QVGA"),
    ("CIF 400x296", "CIF"),
    ("HVGA 480x320", "HVGA"),
    ("VGA 640x480", "VGA"),
    ("SVGA 800x600", "SVGA"),
    ("XGA 1024x768", "XGA"),
    ("HD 1280x720", "HD"),
    ("SXGA 1280x1024", "SXGA"),
    ("UXGA 1600x1200", "UXGA"),
]
TIMER_CAMERA_FRAME_SIZE_LABELS = tuple(label for label, _value in TIMER_CAMERA_FRAME_SIZES)
TIMER_CAMERA_FRAME_SIZE_VALUES = dict(TIMER_CAMERA_FRAME_SIZES)
TIMER_CAMERA_DEFAULT_FRAME_SIZE = "VGA 640x480"
TIMER_CAMERA_DEFAULT_JPEG_QUALITY = "12"
TIMER_CAMERA_DEFAULT_STREAM_DELAY_MS = "100"

ESP32_FRAME_SIZES = [
    ("96x96", 96, 96, 0),
    ("QQVGA 160x120", 160, 120, 1),
    ("QCIF 176x144", 176, 144, 2),
    ("HQVGA 240x176", 240, 176, 3),
    ("240x240", 240, 240, 4),
    ("QVGA 320x240", 320, 240, 5),
    ("CIF 400x296", 400, 296, 6),
    ("HVGA 480x320", 480, 320, 7),
    ("VGA 640x480", 640, 480, 8),
    ("SVGA 800x600", 800, 600, 9),
    ("XGA 1024x768", 1024, 768, 10),
    ("HD 1280x720", 1280, 720, 11),
    ("SXGA 1280x1024", 1280, 1024, 12),
    ("UXGA 1600x1200", 1600, 1200, 13),
]


@dataclass(frozen=True)
class CameraSettings:
    source_kind: str
    source: str
    width: int
    height: int
    fps: float
    resize_display: bool


@dataclass(frozen=True)
class OcrAttempt:
    path: Path
    text: str
    language: str
    confidence: float
    psm: int
    score: float
    engine: str = "tesseract"


@dataclass(frozen=True)
class OcrSavedOutput:
    result_path: Path
    transcript_path: Path
    corrected_text: str
    raw_text: str
    confidence: float
    best_path: Path
    menu_match_count: int
    menu_item_count: int


def nearest_esp32_framesize(width: int, height: int) -> tuple[str, int, int, int]:
    target_ratio = width / height
    target_area = width * height

    def score(item: tuple[str, int, int, int]) -> float:
        _, item_width, item_height, _ = item
        ratio_penalty = abs((item_width / item_height) - target_ratio) * 500_000
        area_penalty = abs((item_width * item_height) - target_area)
        return ratio_penalty + area_penalty

    return min(ESP32_FRAME_SIZES, key=score)


def derive_control_url(stream_url: str) -> str:
    parts = urlsplit(stream_url)
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    if parts.port == 81:
        netloc = hostname
    else:
        netloc = parts.netloc

    return urlunsplit((parts.scheme or "http", netloc, "/control", "", ""))


def with_query(url: str, **params: object) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _split_http_value(value: str) -> tuple[str, str]:
    raw = value.strip().rstrip("/")
    if not raw:
        raise ValueError("Enter the TimerCamera-F IP address or URL.")
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"

    parts = urlsplit(raw)
    if not parts.netloc:
        raise ValueError("Enter a valid TimerCamera-F IP address or URL.")
    return parts.scheme or "http", parts.netloc


def build_timercamera_endpoint_url(value: str, endpoint: str) -> str:
    scheme, netloc = _split_http_value(value)
    path = "/" + endpoint.strip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def build_timercamera_stream_url(value: str) -> str:
    return build_timercamera_endpoint_url(value, "stream")


def build_timercamera_capture_url(value: str) -> str:
    return build_timercamera_endpoint_url(value, "capture.jpg")


def build_timercamera_status_url(value: str) -> str:
    return build_timercamera_endpoint_url(value, "status")


def build_timercamera_control_url(value: str) -> str:
    return build_timercamera_endpoint_url(value, "control")


def request_http_image(url: str, timeout: float = 5.0, max_bytes: int = 8 * 1024 * 1024) -> Image.Image:
    with urlopen(url, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError(f"HTTP image is too large: more than {max_bytes} bytes")
    if "image" not in content_type.lower() and not data.startswith(b"\xff\xd8"):
        raise RuntimeError(f"HTTP response is not an image: {content_type or 'unknown content type'}")

    image = Image.open(io.BytesIO(data))
    image.load()
    return image.convert("RGB")


def request_json(url: str, timeout: float = 3.0) -> dict[str, object]:
    with urlopen(url, timeout=timeout) as response:
        data = response.read(256 * 1024)
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("HTTP JSON response is not an object")
    return parsed


def _status_bool(value: object) -> str:
    return "ok" if bool(value) else "no"


def _format_uptime(milliseconds: object) -> str:
    try:
        total_seconds = max(0, int(milliseconds) // 1000)
    except (TypeError, ValueError):
        return "unknown"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_timercamera_status(data: dict[str, object]) -> str:
    device = str(data.get("device") or "TimerCamera-F")
    ip = str(data.get("ip") or "no-ip")
    frame_size = str(data.get("frame_size") or "unknown")
    frame_width = data.get("frame_width")
    frame_height = data.get("frame_height")
    quality = data.get("jpeg_quality", "?")
    delay_ms = data.get("stream_delay_ms", "?")
    rssi = data.get("rssi", "?")
    frames = data.get("frames_sent", 0)
    last_bytes = data.get("last_frame_bytes", 0)
    battery = data.get("battery_adc_raw", "?")
    dimensions = f"{frame_width}x{frame_height}" if frame_width and frame_height else "unknown"
    return (
        f"{device} | camera {_status_bool(data.get('camera_ready'))} | psram {_status_bool(data.get('psram_found'))} | "
        f"wifi {ip} ({rssi} dBm) | {frame_size} {dimensions} | q{quality} | delay {delay_ms}ms | "
        f"frames {frames} | last {last_bytes} bytes | battery ADC {battery} | uptime {_format_uptime(data.get('uptime_ms'))}"
    )


def _auto_crop_gray(gray: np.ndarray) -> np.ndarray:
    threshold = max(28, int(np.percentile(gray, 42)))
    mask = (gray > threshold).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    points = cv2.findNonZero(mask)
    if points is None:
        return gray

    x, y, width, height = cv2.boundingRect(points)
    image_height, image_width = gray.shape[:2]
    area_ratio = (width * height) / float(image_width * image_height)
    if area_ratio < 0.18 or area_ratio > 0.96:
        return gray

    margin_x = int(image_width * 0.025)
    margin_y = int(image_height * 0.025)
    x0 = max(0, x - margin_x)
    y0 = max(0, y - margin_y)
    x1 = min(image_width, x + width + margin_x)
    y1 = min(image_height, y + height + margin_y)
    return gray[y0:y1, x0:x1]


def _prepare_ocr_gray(
    image: Image.Image,
    crop: bool = False,
    target_long_edge: int = OCR_FAST_LONG_EDGE,
) -> np.ndarray:
    rgb = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    if crop:
        gray = _auto_crop_gray(gray)

    height, width = gray.shape[:2]
    scale = max(1.0, target_long_edge / max(width, height))
    if scale > 1.0:
        gray = cv2.resize(
            gray,
            (int(width * scale), int(height * scale)),
            interpolation=cv2.INTER_CUBIC,
        )

    denoised = cv2.fastNlMeansDenoising(gray, None, 5, 7, 21)
    clahe = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8)).apply(denoised)
    blur = cv2.GaussianBlur(clahe, (0, 0), 1.0)
    sharpened = cv2.addWeighted(clahe, 1.9, blur, -0.9, 0)
    return cv2.normalize(sharpened, None, 0, 255, cv2.NORM_MINMAX)


def _binary_ocr_images(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        51,
        11,
    )
    mean_binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY,
        45,
        9,
    )
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary, mean_binary, otsu


def build_ocr_candidate_images(image: Image.Image, high_accuracy: bool = False) -> dict[str, Image.Image]:
    target_long_edge = OCR_ACCURATE_LONG_EDGE if high_accuracy else OCR_FAST_LONG_EDGE
    gray = _prepare_ocr_gray(image, crop=False, target_long_edge=target_long_edge)
    binary, mean_binary, otsu = _binary_ocr_images(gray)

    candidates = {
        "ocr_gray": Image.fromarray(gray),
        "ocr_otsu": Image.fromarray(otsu),
    }
    if high_accuracy:
        candidates["ocr_bw"] = Image.fromarray(binary)
        candidates["ocr_mean_bw"] = Image.fromarray(mean_binary)

    cropped_gray = _prepare_ocr_gray(image, crop=True, target_long_edge=target_long_edge)
    if high_accuracy and cropped_gray.shape != gray.shape:
        cropped_binary, _, cropped_otsu = _binary_ocr_images(cropped_gray)
        candidates["ocr_crop_gray"] = Image.fromarray(cropped_gray)
        candidates["ocr_crop_bw"] = Image.fromarray(cropped_binary)
        candidates["ocr_crop_otsu"] = Image.fromarray(cropped_otsu)

    return candidates


def enhance_for_ocr(image: Image.Image) -> tuple[Image.Image, Image.Image]:
    candidates = build_ocr_candidate_images(image)
    return candidates["ocr_gray"], candidates["ocr_otsu"]


def find_tesseract() -> Path | None:
    env_cmd = os.environ.get("TESSERACT_CMD", "").strip().strip('"')
    if env_cmd:
        env_path = Path(env_cmd)
        if env_path.is_file():
            return env_path

    from_path = shutil.which("tesseract")
    if from_path:
        return Path(from_path)

    for path in TESSERACT_PATHS:
        if path.is_file():
            return path

    return None


def configure_tesseract() -> Path:
    tesseract = find_tesseract()
    if tesseract is None:
        raise RuntimeError(
            "Tesseract OCR was not found. "
            "Run `winget install --id UB-Mannheim.TesseractOCR`."
        )

    pytesseract.pytesseract.tesseract_cmd = str(tesseract)
    if TESSDATA_DIR.exists():
        os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)
    return tesseract


def available_ocr_languages() -> set[str]:
    if not TESSDATA_DIR.exists():
        return set()
    return {path.stem for path in TESSDATA_DIR.glob("*.traineddata")}


def normalize_ocr_language(language: str) -> str:
    available = available_ocr_languages()
    requested = [part for part in language.split("+") if part]
    usable = [part for part in requested if part in available]
    if usable:
        return "+".join(usable)
    if "eng" in available:
        return "eng"
    return language or "eng"


def normalize_ocr_engine(engine: str) -> str:
    value = engine.strip().lower()
    if value in OCR_ENGINES:
        return value
    return DEFAULT_OCR_ENGINE


def transcribe_ocr_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]

    cleaned: list[str] = []
    previous_empty = False
    for line in lines:
        line = re.sub(r"(?<=[\u3040-\u30ff\u3400-\u9fff])\s+(?=[\u3040-\u30ff\u3400-\u9fff])", "", line)
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            if not previous_empty and cleaned:
                cleaned.append("")
            previous_empty = True
            continue
        cleaned.append(line)
        previous_empty = False

    return "\n".join(cleaned).strip()


def average_confidence(data: dict[str, list[object]]) -> float:
    values: list[float] = []
    for raw in data.get("conf", []):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            values.append(value)
    if not values:
        return 0.0
    return sum(values) / len(values)


def ocr_data_to_text(data: dict[str, list[object]]) -> str:
    rows: list[tuple[int, int, int, int, str]] = []
    texts = data.get("text", [])
    for index, raw_text in enumerate(texts):
        text = str(raw_text).strip()
        if not text:
            continue
        try:
            block = int(data.get("block_num", [0])[index])
            paragraph = int(data.get("par_num", [0])[index])
            line = int(data.get("line_num", [0])[index])
        except (IndexError, TypeError, ValueError):
            block = paragraph = 0
            line = index
        rows.append((block, paragraph, line, index, text))

    lines: list[str] = []
    current_key: tuple[int, int, int] | None = None
    current_words: list[str] = []
    for block, paragraph, line, _index, text in rows:
        key = (block, paragraph, line)
        if current_key is not None and key != current_key:
            lines.append(" ".join(current_words))
            current_words = []
        current_key = key
        current_words.append(text)

    if current_words:
        lines.append(" ".join(current_words))
    return "\n".join(lines).strip()


def run_tesseract_ocr(image_path: Path, language: str, psm: int = 6) -> tuple[str, float, str]:
    configure_tesseract()
    language = normalize_ocr_language(language)
    config_parts = []
    if TESSDATA_DIR.exists():
        config_parts.extend(["--tessdata-dir", str(TESSDATA_DIR)])
    config_parts.extend(
        [
            "--oem",
            "1",
            "--psm",
            str(psm),
            "-c",
            "preserve_interword_spaces=1",
            "-c",
            "user_defined_dpi=300",
        ]
    )
    config = " ".join(config_parts)

    with Image.open(image_path) as image:
        data = pytesseract.image_to_data(
            image,
            lang=language,
            config=config,
            output_type=pytesseract.Output.DICT,
        )
    return ocr_data_to_text(data), average_confidence(data), language


def get_rapidocr_engine() -> RapidOCR:
    global _RAPIDOCR_ENGINE
    if _RAPIDOCR_ENGINE is None:
        _RAPIDOCR_ENGINE = RapidOCR(
            params={
                "Global.log_level": "warning",
                "Global.use_cls": False,
                "Rec.lang_type": LangRec.JAPAN,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.ocr_version": OCRVersion.PPOCRV4,
            }
        )
    return _RAPIDOCR_ENGINE


def run_rapidocr_ocr(image_path: Path) -> OcrAttempt:
    result = get_rapidocr_engine()(str(image_path), use_cls=False)
    texts = [str(text).strip() for text in (getattr(result, "txts", None) or [])]
    texts = [text for text in texts if text]
    raw_scores = getattr(result, "scores", None) or []
    scores = []
    for raw_score in raw_scores:
        try:
            scores.append(float(raw_score) * 100.0)
        except (TypeError, ValueError):
            continue

    text = "\n".join(texts)
    confidence = (sum(scores) / len(scores)) if scores else 0.0
    score = _score_ocr_text(text, confidence)
    return OcrAttempt(
        path=image_path,
        text=text,
        language="japan",
        confidence=confidence,
        psm=0,
        score=score,
        engine="rapidocr",
    )


def _score_ocr_text(text: str, confidence: float) -> float:
    useful_chars = len(re.sub(r"\s+", "", text))
    if useful_chars == 0:
        return -1000.0

    japanese_chars = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", text))
    junk_chars = len(
        re.findall(
            r"[^\w\s\u3040-\u30ff\u3400-\u9fff"
            r"\u3002\uff0c\uff0e\u30fb\u30fc\-+:/()"
            r"\uff08\uff09\u300c\u300d\u300e\u300f\u3010\u3011]",
            text,
        )
    )
    line_count = len([line for line in text.splitlines() if line.strip()])
    short_penalty = 20.0 if useful_chars < 4 else 0.0
    return (
        confidence
        + min(useful_chars, 350) * 0.14
        + min(japanese_chars, 250) * 0.08
        + min(line_count, 8) * 0.5
        - junk_chars * 1.2
        - short_penalty
    )


def choose_best_ocr_result(
    paths: list[Path],
    language: str,
    high_accuracy: bool = False,
    engine: str = DEFAULT_OCR_ENGINE,
) -> OcrAttempt:
    engine = normalize_ocr_engine(engine)
    existing_paths = [path for path in paths if path.exists()]
    if not existing_paths:
        raise RuntimeError("No image is available for OCR.")

    if engine == "rapidocr":
        raw_paths = [path for path in existing_paths if path.stem.endswith("_raw") or "_raw" in path.stem]
        return run_rapidocr_ocr(raw_paths[0] if raw_paths else existing_paths[0])

    best: OcrAttempt | None = None
    psm_modes = OCR_ACCURATE_PSM_MODES if high_accuracy else OCR_FAST_PSM_MODES
    tasks = [(path, psm) for path in existing_paths for psm in psm_modes]

    def attempt_ocr(path: Path, psm: int) -> OcrAttempt:
        text, confidence, used_language = run_tesseract_ocr(path, language, psm=psm)
        score = _score_ocr_text(text, confidence)
        return OcrAttempt(path, text, used_language, confidence, psm, score, engine="tesseract")

    worker_count = min(len(tasks), max(1, (os.cpu_count() or 4) - 1), 8)
    if worker_count <= 1:
        for path, psm in tasks:
            attempt = attempt_ocr(path, psm)
            if best is None or attempt.score > best.score:
                best = attempt
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(attempt_ocr, path, psm) for path, psm in tasks]
            for future in as_completed(futures):
                attempt = future.result()
                if best is None or attempt.score > best.score:
                    best = attempt

    if best is None:
        raise RuntimeError("No image is available for OCR.")
    return best


def save_ocr_outputs(
    raw_path: Path,
    candidate_paths: list[Path],
    language: str,
    high_accuracy: bool = False,
    engine: str = DEFAULT_OCR_ENGINE,
) -> OcrSavedOutput:
    best = choose_best_ocr_result(
        candidate_paths,
        language,
        high_accuracy=high_accuracy,
        engine=engine,
    )
    best_path = best.path
    text = best.text
    used_language = best.language
    confidence = best.confidence
    raw_transcript = transcribe_ocr_text(text)
    selection = select_best_menu_item(raw_transcript, MENU_DATABASE_PATH)
    line_correction = correct_ocr_text(raw_transcript, MENU_DATABASE_PATH)
    corrected_transcript = selection.selected_name
    base = raw_path.with_suffix("")
    result_path = base.with_name(base.name + "_ocr_result.txt")
    transcript_path = base.with_name(base.name + "_transcript.md")
    engine_label = "RapidOCR ONNX" if best.engine == "rapidocr" else "Tesseract"
    psm_label = str(best.psm) if best.engine == "tesseract" else "n/a"
    database_label = (
        f"`{MENU_DATABASE_PATH.name}` ({selection.item_count} items)"
        if selection.database_loaded
        else "not found"
    )
    selected_item = selection.selected_item
    selected_name = selection.selected_name or "(no menu item selected)"
    selected_score = f"{selection.score:.2f}" if selected_item else "n/a"
    selected_price = f"{selected_item.price} JPY" if selected_item and selected_item.price is not None else "n/a"
    selected_type = selected_item.item_type if selected_item else "n/a"
    detected_price_text = ", ".join(str(price) for price in selection.detected_prices) or "none"
    line_match_lines = [
        f"- {match.source_line} -> {match.corrected_line} (score {match.score:.2f})"
        for match in line_correction.matches
    ]
    raw_text_lines = [
        "",
        "## Raw OCR Text",
        "",
        raw_transcript or "(no text detected)",
    ]
    selection_lines = [
        "",
        "## Selected Menu Item",
        "",
        f"- Name: {selected_name}",
        f"- Score: {selected_score}",
        f"- Full text score: {selection.full_text_score:.2f}",
        f"- Best line score: {selection.line_score:.2f}",
        f"- Best matched line: {selection.matched_line or 'n/a'}",
        f"- Detected prices: {detected_price_text}",
        f"- Price: {selected_price}",
        f"- Type: {selected_type}",
    ]
    line_diagnostic_lines = []
    if line_match_lines:
        line_diagnostic_lines = [
            "",
            "## Line Correction Diagnostics",
            "",
            *line_match_lines,
        ]

    result_path.write_text(
        corrected_transcript + ("\n" if corrected_transcript else ""),
        encoding="utf-8",
    )
    transcript_path.write_text(
        "\n".join(
            [
                "# OCR Transcript",
                "",
                f"- Source image: `{raw_path.name}`",
                f"- OCR image: `{best_path.name}`",
                f"- OCR engine: `{engine_label}`",
                f"- Language: `{used_language}`",
                f"- Tesseract PSM: `{psm_label}`",
                f"- Average confidence: `{confidence:.1f}`",
                f"- Menu database: {database_label}",
                f"- Selected menu item: `{selected_name}`",
                f"- Selected menu score: `{selected_score}`",
                f"- Menu matches: `{1 if selected_item else 0}`",
                "",
                "## Corrected Text",
                "",
                corrected_transcript or "(no menu item selected)",
                *selection_lines,
                *raw_text_lines,
                *line_diagnostic_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )

    return OcrSavedOutput(
        result_path=result_path,
        transcript_path=transcript_path,
        corrected_text=corrected_transcript,
        raw_text=raw_transcript,
        confidence=confidence,
        best_path=best_path,
        menu_match_count=1 if selected_item else 0,
        menu_item_count=selection.item_count,
    )


def recv_line(sock: socket.socket, max_len: int = 256) -> bytes:
    chunks = bytearray()
    while len(chunks) < max_len:
        chunk = sock.recv(1)
        if not chunk:
            break
        if chunk == b"\n":
            return bytes(chunks)
        if chunk != b"\r":
            chunks.extend(chunk)
    return bytes(chunks)


def recv_exact(sock: socket.socket, byte_count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < byte_count:
        chunk = sock.recv(min(64 * 1024, byte_count - len(chunks)))
        if not chunk:
            raise RuntimeError("TCP receive ended before the full payload was read.")
        chunks.extend(chunk)
    return bytes(chunks)


def request_tcp_still_image(host: str, port: int, timeout: float = 12.0) -> Image.Image:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(b"CAPTURE\n")
        header = recv_line(sock).decode("utf-8", errors="replace").strip()
        if header.startswith("ERR "):
            raise RuntimeError(f"ESP32 error: {header[4:]}")

        parts = header.split()
        if len(parts) != 4 or parts[0] != "OK":
            raise RuntimeError(f"Invalid TCP response: {header!r}")

        try:
            jpeg_len = int(parts[1])
            width = int(parts[2])
            height = int(parts[3])
        except ValueError as exc:
            raise RuntimeError(f"Invalid TCP response size fields: {header!r}") from exc

        if jpeg_len <= 0 or jpeg_len > TCP_STILL_MAX_BYTES:
            raise RuntimeError(f"Invalid JPEG size: {jpeg_len} bytes")

        jpeg = recv_exact(sock, jpeg_len)

    image = Image.open(io.BytesIO(jpeg))
    image.load()
    image = image.convert("RGB")
    if image.size != (width, height):
        image.info["esp32_reported_size"] = (width, height)
    return image


def ping_tcp_still(host: str, port: int, timeout: float = 3.0) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(b"PING\n")
        return recv_line(sock).decode("utf-8", errors="replace").strip()


class UsbStillClient:
    def __init__(self, port: str, baudrate: int) -> None:
        self.port = port
        self.baudrate = baudrate
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()
        self._request_id = 0

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                finally:
                    self._serial = None

    def ping(self, timeout: float = 8.0) -> str:
        with self._lock:
            ser = self._open_locked()
            deadline = time.monotonic() + timeout
            last_line = ""
            while time.monotonic() < deadline:
                self._drop_pending_input(ser)
                ser.write(b"PING\n")
                ser.flush()
                line = self._read_protocol_line_locked(ser, min(1.2, max(0.2, deadline - time.monotonic())))
                if line.startswith(("OK ", "ERR ")):
                    return line
                if line:
                    last_line = line
                time.sleep(0.2)
            raise RuntimeError(f"No USB response. Last line: {last_line!r}")

    def capture(self, timeout: float = 20.0) -> Image.Image:
        with self._lock:
            ser = self._open_locked()
            self._request_id = (self._request_id + 1) & 0xFFFFFFFF
            request_id = str(self._request_id)
            self._drop_pending_input(ser)
            self._drop_pending_output(ser)
            ser.write(f"CAPTURE {request_id}\n".encode("ascii"))
            ser.flush()

            jpeg_len, width, height = self._read_capture_header_locked(ser, request_id, timeout)

            jpeg = self._read_exact_locked(ser, jpeg_len, timeout)
            self._drop_pending_input(ser)

        image = Image.open(io.BytesIO(jpeg))
        image.load()
        image = image.convert("RGB")
        if image.size != (width, height):
            image.info["esp32_reported_size"] = (width, height)
        return image

    def _open_locked(self) -> serial.Serial:
        if self._serial is not None and self._serial.is_open:
            return self._serial

        ser = serial.Serial(
            self.port,
            self.baudrate,
            timeout=0.15,
            write_timeout=5.0,
        )
        ser.dtr = False
        ser.rts = False
        self._serial = ser
        time.sleep(0.3)
        return ser

    @staticmethod
    def _drop_pending_input(ser: serial.Serial) -> None:
        try:
            ser.reset_input_buffer()
        except serial.SerialException:
            pass

    @staticmethod
    def _drop_pending_output(ser: serial.Serial) -> None:
        try:
            ser.reset_output_buffer()
        except serial.SerialException:
            pass

    def _read_capture_header_locked(
        self,
        ser: serial.Serial,
        request_id: str,
        timeout: float,
    ) -> tuple[int, int, int]:
        deadline = time.monotonic() + timeout
        last_header = ""
        while time.monotonic() < deadline:
            header = self._read_protocol_line_locked(ser, max(0.05, deadline - time.monotonic()))
            if header.startswith("ERR "):
                raise RuntimeError(f"ESP32 error: {header[4:]}")

            parts = header.split()
            last_header = header or last_header
            if len(parts) == 4 and parts[0] == "OK":
                raise RuntimeError("The ESP32 USB still firmware is outdated. Flash the newer firmware.")

            if len(parts) != 5 or parts[0] != "OK":
                continue

            response_id = parts[1]
            try:
                jpeg_len = int(parts[2])
                width = int(parts[3])
                height = int(parts[4])
            except ValueError:
                continue

            if jpeg_len <= 0 or jpeg_len > USB_STILL_MAX_BYTES:
                raise RuntimeError(f"Invalid JPEG size: {jpeg_len} bytes")

            if response_id == request_id:
                return jpeg_len, width, height

            self._read_exact_locked(ser, jpeg_len, max(0.05, deadline - time.monotonic()))

        raise RuntimeError(f"No USB response for request ID {request_id}. Last line: {last_header!r}")

    def _read_protocol_line_locked(self, ser: serial.Serial, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        last_line = ""
        while time.monotonic() < deadline:
            line = self._read_line_locked(ser, max(0.05, deadline - time.monotonic()))
            if line.startswith(("OK ", "ERR ")):
                return line
            if line:
                last_line = line
        return last_line

    @staticmethod
    def _read_line_locked(ser: serial.Serial, timeout: float, max_len: int = 512) -> str:
        deadline = time.monotonic() + timeout
        chunks = bytearray()
        while time.monotonic() < deadline and len(chunks) < max_len:
            chunk = ser.read(1)
            if not chunk:
                continue
            if chunk == b"\n":
                return chunks.decode("utf-8", errors="replace").strip()
            if chunk != b"\r":
                chunks.extend(chunk)
        return chunks.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _read_exact_locked(ser: serial.Serial, byte_count: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        chunks = bytearray()
        while len(chunks) < byte_count and time.monotonic() < deadline:
            chunk = ser.read(min(64 * 1024, byte_count - len(chunks)))
            if chunk:
                chunks.extend(chunk)
        if len(chunks) != byte_count:
            raise RuntimeError(f"USB receive stopped early: {len(chunks)}/{byte_count} bytes")
        return bytes(chunks)


class VideoWorker(threading.Thread):
    def __init__(
        self,
        settings: CameraSettings,
        frame_queue: queue.Queue[Image.Image],
        status_queue: queue.Queue[str],
    ) -> None:
        super().__init__(daemon=True)
        self.settings = settings
        self.frame_queue = frame_queue
        self.status_queue = status_queue
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        source: int | str
        if self.settings.source_kind == "usb":
            source = int(self.settings.source)
            capture = cv2.VideoCapture(source, cv2.CAP_DSHOW)
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.settings.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.settings.height)
            capture.set(cv2.CAP_PROP_FPS, self.settings.fps)
        else:
            source = self.settings.source
            capture = cv2.VideoCapture(source)

        if not capture.isOpened():
            self.status_queue.put("Could not open the camera. Check the URL or index.")
            capture.release()
            return

        self.status_queue.put("Connected.")
        frame_interval = 1.0 / self.settings.fps if self.settings.fps > 0 else 0.0
        last_notice = 0.0

        try:
            while not self._stop_event.is_set():
                start = time.perf_counter()
                ok, frame = capture.read()

                if not ok or frame is None:
                    now = time.monotonic()
                    if now - last_notice > 2.0:
                        self.status_queue.put("Waiting for frames.")
                        last_notice = now
                    time.sleep(0.15)
                    continue

                if self.settings.resize_display:
                    frame = cv2.resize(
                        frame,
                        (self.settings.width, self.settings.height),
                        interpolation=cv2.INTER_AREA,
                    )

                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(frame)
                self._put_latest(image)

                if frame_interval:
                    elapsed = time.perf_counter() - start
                    if elapsed < frame_interval:
                        time.sleep(frame_interval - elapsed)
        finally:
            capture.release()
            self.status_queue.put("Stopped.")

    def _put_latest(self, image: Image.Image) -> None:
        try:
            while self.frame_queue.full():
                self.frame_queue.get_nowait()
            self.frame_queue.put_nowait(image)
        except queue.Full:
            pass


class TimerCameraHttpWorker(threading.Thread):
    def __init__(
        self,
        settings: CameraSettings,
        frame_queue: queue.Queue[Image.Image],
        status_queue: queue.Queue[str],
    ) -> None:
        super().__init__(daemon=True)
        self.settings = settings
        self.capture_url = build_timercamera_capture_url(settings.source)
        self.frame_queue = frame_queue
        self.status_queue = status_queue
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        frame_interval = 1.0 / self.settings.fps if self.settings.fps > 0 else 0.0
        timeout = max(2.0, frame_interval + 1.0)
        last_notice = 0.0
        connected_notice_sent = False

        while not self._stop_event.is_set():
            start = time.perf_counter()
            try:
                image = request_http_image(self.capture_url, timeout=timeout)
                if self.settings.resize_display:
                    image = image.resize((self.settings.width, self.settings.height), Image.Resampling.LANCZOS)
                self._put_latest(image)
                if not connected_notice_sent:
                    self.status_queue.put("TimerCamera-F live capture connected.")
                    connected_notice_sent = True
            except Exception as exc:
                now = time.monotonic()
                if now - last_notice > 2.0:
                    self.status_queue.put(f"TimerCamera-F live capture waiting: {exc}")
                    last_notice = now
                time.sleep(0.25)

            if frame_interval:
                elapsed = time.perf_counter() - start
                if elapsed < frame_interval:
                    time.sleep(frame_interval - elapsed)

        self.status_queue.put("TimerCamera-F live capture stopped.")

    def _put_latest(self, image: Image.Image) -> None:
        try:
            while self.frame_queue.full():
                self.frame_queue.get_nowait()
            self.frame_queue.put_nowait(image)
        except queue.Full:
            pass


class UdpVideoWorker(threading.Thread):
    def __init__(
        self,
        settings: CameraSettings,
        listen_port: int,
        control_port: int,
        frame_queue: queue.Queue[Image.Image],
        status_queue: queue.Queue[str],
    ) -> None:
        super().__init__(daemon=True)
        self.settings = settings
        self.listen_port = listen_port
        self.control_port = control_port
        self.frame_queue = frame_queue
        self.status_queue = status_queue
        self._stop_event = threading.Event()
        self._device_addr: tuple[str, int] | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._last_control_target: str | None = None

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def send_control(self, width: int, height: int, fps: float) -> bool:
        with self._lock:
            sock = self._sock
            device_addr = self._device_addr

        if sock is None or device_addr is None:
            self.status_queue.put("UDP control target is not detected. Apply again after video or HELLO reception.")
            return False

        target = (device_addr[0], self.control_port)
        commands = [
            f"HOST {self._local_ip_for(device_addr[0])}\n",
            f"SET {width} {height} {fps:.2f}\n",
        ]

        try:
            for command in commands:
                sock.sendto(command.encode("ascii"), target)
        except OSError as exc:
            self.status_queue.put(f"UDP control send failed: {exc}")
            return False

        self._last_control_target = device_addr[0]
        self.status_queue.put(f"UDP control sent: {width}x{height} @ {fps:g}fps -> {device_addr[0]}")
        return True

    def run(self) -> None:
        frames: dict[int, dict[str, object]] = {}
        last_frame_notice = 0.0

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.listen_port))
        sock.settimeout(0.2)

        with self._lock:
            self._sock = sock

        self.status_queue.put(f"UDP listening on 0.0.0.0:{self.listen_port}")

        try:
            while not self._stop_event.is_set():
                self._drop_stale_frames(frames)
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break

                self._remember_device(addr)

                if not data.startswith(UDP_MAGIC):
                    self._handle_text_status(data, addr)
                    continue

                if len(data) < UDP_HEADER.size:
                    continue

                (
                    magic,
                    version,
                    packet_type,
                    header_len,
                    frame_id,
                    packet_index,
                    packet_count,
                    frame_len,
                    payload_len,
                    width,
                    height,
                    fps_x10,
                ) = UDP_HEADER.unpack_from(data)

                if (
                    magic != UDP_MAGIC
                    or version != 1
                    or packet_type != UDP_PACKET_TYPE_FRAME
                    or header_len < UDP_HEADER.size
                    or packet_count == 0
                    or packet_index >= packet_count
                ):
                    continue

                payload = data[header_len : header_len + payload_len]
                if len(payload) != payload_len:
                    continue

                state = frames.get(frame_id)
                if state is None:
                    state = {
                        "created": time.monotonic(),
                        "packets": [None] * packet_count,
                        "received": 0,
                        "frame_len": frame_len,
                        "packet_count": packet_count,
                        "width": width,
                        "height": height,
                        "fps_x10": fps_x10,
                    }
                    frames[frame_id] = state

                packets = state["packets"]
                if not isinstance(packets, list) or len(packets) != packet_count:
                    continue

                if packets[packet_index] is None:
                    packets[packet_index] = payload
                    state["received"] = int(state["received"]) + payload_len

                if int(state["received"]) >= frame_len and all(part is not None for part in packets):
                    jpeg = b"".join(part for part in packets if isinstance(part, bytes))
                    del frames[frame_id]
                    if len(jpeg) != frame_len:
                        continue
                    self._publish_jpeg(jpeg)
                    now = time.monotonic()
                    if now - last_frame_notice > 1.0:
                        fps = fps_x10 / 10 if fps_x10 else 0
                        self.status_queue.put(
                            f"UDP receiving: {width}x{height}, device={addr[0]}, target={fps:g}fps"
                        )
                        last_frame_notice = now
        finally:
            with self._lock:
                self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            self.status_queue.put("UDP listener stopped.")

    def _remember_device(self, addr: tuple[str, int]) -> None:
        send_initial_control = False
        with self._lock:
            previous = self._device_addr[0] if self._device_addr else None
            self._device_addr = addr
            if previous != addr[0] or self._last_control_target != addr[0]:
                send_initial_control = True

        if send_initial_control:
            self.send_control(self.settings.width, self.settings.height, self.settings.fps)

    def _handle_text_status(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            text = data.decode("utf-8", errors="replace").strip()
        except UnicodeDecodeError:
            return
        if text:
            self.status_queue.put(f"{addr[0]}: {text}")

    def _publish_jpeg(self, jpeg: bytes) -> None:
        try:
            image = Image.open(io.BytesIO(jpeg))
            image.load()
            image = image.convert("RGB")
        except OSError:
            return

        if self.settings.resize_display:
            image = image.resize((self.settings.width, self.settings.height), Image.Resampling.LANCZOS)

        try:
            while self.frame_queue.full():
                self.frame_queue.get_nowait()
            self.frame_queue.put_nowait(image)
        except queue.Full:
            pass

    def _drop_stale_frames(self, frames: dict[int, dict[str, object]]) -> None:
        now = time.monotonic()
        stale = [
            frame_id
            for frame_id, state in frames.items()
            if now - float(state.get("created", now)) > UDP_STALE_FRAME_SECONDS
        ]
        for frame_id in stale:
            frames.pop(frame_id, None)

    @staticmethod
    def _local_ip_for(remote_ip: str) -> str:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((remote_ip, 9))
            return probe.getsockname()[0]
        except OSError:
            return "0.0.0.0"
        finally:
            probe.close()


class VrcUdpVideoWorker(threading.Thread):
    def __init__(
        self,
        settings: CameraSettings,
        esp32_port: int,
        frame_queue: queue.Queue[Image.Image],
        status_queue: queue.Queue[str],
    ) -> None:
        super().__init__(daemon=True)
        self.settings = settings
        self.esp32_port = esp32_port
        self.frame_queue = frame_queue
        self.status_queue = status_queue
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._last_heartbeat = 0.0
        self._last_frame_id: int | None = None
        self._drop_count = 0
        self._frame_count = 0
        self._fps_window_start = time.monotonic()
        self._fps_window_frames = 0
        self._last_publish = 0.0

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def run(self) -> None:
        target = (self.settings.source, self.esp32_port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, VRC_RECV_BUF_SIZE)
        sock.bind(("", 0))
        sock.settimeout(0.1)
        self._sock = sock

        self.status_queue.put(f"VRC UDP connecting: {target[0]}:{target[1]}")
        self._send_command(VRC_CMD_REGISTER)
        frame_interval = 1.0 / self.settings.fps if self.settings.fps > 0 else 0.0
        next_frame_at = 0.0

        try:
            while not self._stop_event.is_set():
                self._heartbeat_if_needed()

                latest = self._read_latest_packet(sock)
                if latest is None:
                    continue

                now = time.perf_counter()
                if frame_interval and now < next_frame_at:
                    continue
                if frame_interval:
                    next_frame_at = now + frame_interval

                self._publish_packet(latest)
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None
            self.status_queue.put("VRC UDP stopped.")

    def _send_command(self, command: bytes) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendto(command, (self.settings.source, self.esp32_port))
        except OSError as exc:
            self.status_queue.put(f"VRC UDP send failed: {exc}")

    def _heartbeat_if_needed(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat >= 1.0:
            self._last_heartbeat = now
            self._send_command(VRC_CMD_HEARTBEAT)

    def _read_latest_packet(self, sock: socket.socket) -> bytes | None:
        latest: bytes | None = None

        try:
            data, _ = sock.recvfrom(65535)
            if len(data) > 2:
                latest = data
        except socket.timeout:
            return None
        except OSError:
            return None

        while True:
            try:
                sock.setblocking(False)
                data, _ = sock.recvfrom(65535)
                if len(data) > 2:
                    latest = data
            except (BlockingIOError, socket.timeout):
                break
            except OSError:
                return latest
            finally:
                sock.settimeout(0.1)

        return latest

    def _publish_packet(self, packet: bytes) -> None:
        frame_id = struct.unpack("<H", packet[:2])[0]
        jpeg = packet[2:]

        if self._last_frame_id is not None:
            expected = (self._last_frame_id + 1) & 0xFFFF
            if frame_id != expected:
                self._drop_count += (frame_id - self._last_frame_id - 1) & 0xFFFF
        self._last_frame_id = frame_id

        now = time.monotonic()
        if self.settings.fps > 0:
            min_interval = 1.0 / self.settings.fps
            if now - self._last_publish < min_interval:
                return
            self._last_publish = now

        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return

        if self.settings.resize_display:
            frame = cv2.resize(
                frame,
                (self.settings.width, self.settings.height),
                interpolation=cv2.INTER_AREA,
            )

        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(frame)

        try:
            while self.frame_queue.full():
                self.frame_queue.get_nowait()
            self.frame_queue.put_nowait(image)
        except queue.Full:
            pass

        self._frame_count += 1
        self._fps_window_frames += 1
        elapsed = now - self._fps_window_start
        if elapsed >= 1.0:
            fps = self._fps_window_frames / elapsed
            self._fps_window_frames = 0
            self._fps_window_start = now
            self.status_queue.put(
                f"VRC UDP receiving: {fps:.1f}fps, frame={self._frame_count}, drop={self._drop_count}"
            )


class CameraViewerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("M5Stack TimerCamera OCR Menu Capture")
        self.root.geometry("1180x840")
        self.root.minsize(860, 620)

        self.frame_queue: queue.Queue[Image.Image] = queue.Queue(maxsize=2)
        self.status_queue: queue.Queue[str] = queue.Queue()
        self.capture_queue: queue.Queue[tuple[str, Image.Image | None, str]] = queue.Queue()
        self.device_status_queue: queue.Queue[tuple[str, dict[str, object] | None, str]] = queue.Queue()
        self.worker: VideoWorker | TimerCameraHttpWorker | UdpVideoWorker | VrcUdpVideoWorker | None = None
        self.usb_client: UsbStillClient | None = None
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.latest_frame: Image.Image | None = None
        self.ocr_queue: queue.Queue[tuple[int, str, str, str]] = queue.Queue()
        self.capture_busy = False
        self.ocr_busy = False
        self.ocr_job_id = 0
        self.pending_ocr: tuple[Path, list[Path], str, str, bool] | None = None
        self.auto_capture_running = False
        self.auto_capture_after_id: str | None = None

        self.source_kind = tk.StringVar(value="timercamera")
        self.stream_url = tk.StringVar(value="")
        self.usb_index = tk.StringVar(value="0")
        self.serial_port = tk.StringVar(value=USB_STILL_DEFAULT_PORT)
        self.serial_baud = tk.StringVar(value=str(USB_STILL_DEFAULT_BAUD))
        self.control_url = tk.StringVar(value="")
        self.udp_listen_port = tk.StringVar(value="5555")
        self.udp_control_port = tk.StringVar(value="5006")
        self.tcp_still_port = tk.StringVar(value=str(TCP_STILL_DEFAULT_PORT))
        self.width = tk.StringVar(value="800")
        self.height = tk.StringVar(value="600")
        self.fps = tk.StringVar(value="2")
        self.timer_frame_size = tk.StringVar(value=TIMER_CAMERA_DEFAULT_FRAME_SIZE)
        self.timer_jpeg_quality = tk.StringVar(value=TIMER_CAMERA_DEFAULT_JPEG_QUALITY)
        self.timer_stream_delay_ms = tk.StringVar(value=TIMER_CAMERA_DEFAULT_STREAM_DELAY_MS)
        self.auto_interval = tk.StringVar(value="0.5")
        self.resize_display = tk.BooleanVar(value=False)
        self.send_control = tk.BooleanVar(value=True)
        self.save_ocr_images = tk.BooleanVar(value=False)
        self.run_ocr_on_capture = tk.BooleanVar(value=True)
        self.high_accuracy_ocr = tk.BooleanVar(value=False)
        self.flip_horizontal = tk.BooleanVar(value=False)
        self.flip_vertical = tk.BooleanVar(value=False)
        self.ocr_language = tk.StringVar(value=DEFAULT_OCR_LANGUAGE)
        self.ocr_engine = tk.StringVar(value=DEFAULT_OCR_ENGINE)
        self.status_text = tk.StringVar(value="Disconnected")
        self.device_status_text = tk.StringVar(value="TimerCamera-F status has not been read yet.")

        self._build_ui()
        self._poll_queues()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, padding=12)
        shell.pack(fill=tk.BOTH, expand=True)

        controls = ttk.LabelFrame(shell, text="Connection Settings", padding=10)
        controls.pack(fill=tk.X)
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)

        source_modes = ttk.Frame(controls)
        source_modes.grid(row=0, column=0, columnspan=4, sticky=tk.W)
        ttk.Radiobutton(
            source_modes,
            text="TimerCamera-F HTTP",
            variable=self.source_kind,
            value="timercamera",
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            source_modes,
            text="XIAO USB(Type-C)",
            variable=self.source_kind,
            value="xiao_usb",
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            source_modes,
            text="XIAO TCP Still",
            variable=self.source_kind,
            value="xiao_tcp",
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            source_modes,
            text="XIAO UDP Live",
            variable=self.source_kind,
            value="vrc_udp",
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            source_modes,
            text="Legacy ESP32-CAM MJPEG",
            variable=self.source_kind,
            value="esp32",
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            source_modes,
            text="USB Camera Index",
            variable=self.source_kind,
            value="usb",
        ).pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(controls, text="Camera address / URL").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(controls, textvariable=self.stream_url).grid(
            row=1, column=1, columnspan=3, sticky=tk.EW, pady=(8, 0)
        )

        ttk.Label(controls, text="USB camera index").grid(row=2, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(controls, width=10, textvariable=self.usb_index).grid(
            row=2, column=1, sticky=tk.W, pady=(8, 0)
        )
        ttk.Label(controls, text="Control URL (optional)").grid(row=2, column=2, sticky=tk.W, pady=(8, 0))
        ttk.Entry(controls, textvariable=self.control_url).grid(
            row=2, column=3, sticky=tk.EW, pady=(8, 0)
        )

        ttk.Label(controls, text="XIAO COM").grid(row=3, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(controls, width=10, textvariable=self.serial_port).grid(
            row=3, column=1, sticky=tk.W, pady=(8, 0)
        )
        ttk.Label(controls, text="USB baud").grid(row=3, column=2, sticky=tk.W, pady=(8, 0))
        ttk.Entry(controls, width=10, textvariable=self.serial_baud).grid(
            row=3, column=3, sticky=tk.W, pady=(8, 0)
        )

        ttk.Label(controls, text="Width").grid(row=4, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(controls, width=10, textvariable=self.width).grid(
            row=4, column=1, sticky=tk.W, pady=(8, 0)
        )
        ttk.Label(controls, text="Height").grid(row=4, column=2, sticky=tk.W, pady=(8, 0))
        ttk.Entry(controls, width=10, textvariable=self.height).grid(
            row=4, column=3, sticky=tk.W, pady=(8, 0)
        )

        ttk.Label(controls, text="Display FPS").grid(row=5, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Entry(controls, width=10, textvariable=self.fps).grid(
            row=5, column=1, sticky=tk.W, pady=(8, 0)
        )
        ttk.Checkbutton(
            controls,
            text="Resize on PC",
            variable=self.resize_display,
        ).grid(row=5, column=2, columnspan=2, sticky=tk.W, pady=(8, 0))

        ttk.Checkbutton(
            controls,
            text="Send nearest ESP32 frame size",
            variable=self.send_control,
        ).grid(row=6, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))

        buttons = ttk.Frame(controls)
        buttons.grid(row=6, column=2, columnspan=2, sticky=tk.E, pady=(8, 0))
        ttk.Button(buttons, text="Connect", command=self.connect).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="Apply and Reconnect", command=self.apply_and_reconnect).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(buttons, text="Stop", command=self.stop).pack(side=tk.LEFT)

        capture = ttk.Frame(controls)
        capture.grid(row=7, column=0, columnspan=4, sticky=tk.EW, pady=(10, 0))
        ttk.Button(capture, text="Capture", command=self.capture_photo).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(capture, text="Start Auto Capture", command=self.start_auto_capture).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(capture, text="Stop Auto", command=self.stop_auto_capture).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(capture, text="Interval Seconds").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Entry(capture, width=6, textvariable=self.auto_interval).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(capture, text="Image OCR", command=self.ocr_existing_image).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(capture, text=f"Save Directory: {CAPTURE_DIR}").pack(side=tk.RIGHT)

        ocr_controls = ttk.Frame(controls)
        ocr_controls.grid(row=8, column=0, columnspan=4, sticky=tk.W, pady=(8, 0))
        ttk.Label(ocr_controls, text="OCR").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Combobox(
            ocr_controls,
            width=10,
            textvariable=self.ocr_engine,
            values=OCR_ENGINES,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            ocr_controls,
            text="Save OCR Images",
            variable=self.save_ocr_images,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            ocr_controls,
            text="OCR on Capture",
            variable=self.run_ocr_on_capture,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            ocr_controls,
            text="High Accuracy OCR",
            variable=self.high_accuracy_ocr,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(ocr_controls, text="Language").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Combobox(
            ocr_controls,
            width=16,
            textvariable=self.ocr_language,
            values=("jpn+eng", "jpn_vert+jpn+eng", "eng"),
        ).pack(side=tk.LEFT)

        orientation = ttk.Frame(controls)
        orientation.grid(row=9, column=0, columnspan=4, sticky=tk.W, pady=(8, 0))
        ttk.Label(orientation, text="Orientation").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            orientation,
            text="Mirror Horizontal",
            variable=self.flip_horizontal,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            orientation,
            text="Flip Vertical",
            variable=self.flip_vertical,
        ).pack(side=tk.LEFT, padx=(0, 8))

        timer_panel = ttk.LabelFrame(shell, text="TimerCamera-F Control", padding=10)
        timer_panel.pack(fill=tk.X, pady=(10, 0))
        timer_panel.columnconfigure(1, weight=1)
        timer_panel.columnconfigure(3, weight=1)

        ttk.Label(timer_panel, text="Status").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(timer_panel, textvariable=self.device_status_text, wraplength=980, anchor=tk.W).grid(
            row=0, column=1, columnspan=5, sticky=tk.EW, padx=(8, 0)
        )

        ttk.Label(timer_panel, text="Frame Size").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Combobox(
            timer_panel,
            width=16,
            textvariable=self.timer_frame_size,
            values=TIMER_CAMERA_FRAME_SIZE_LABELS,
            state="readonly",
        ).grid(row=1, column=1, sticky=tk.W, pady=(8, 0))
        ttk.Label(timer_panel, text="JPEG Quality").grid(row=1, column=2, sticky=tk.W, padx=(12, 0), pady=(8, 0))
        ttk.Spinbox(
            timer_panel,
            from_=4,
            to=63,
            width=6,
            textvariable=self.timer_jpeg_quality,
        ).grid(row=1, column=3, sticky=tk.W, pady=(8, 0))
        ttk.Label(timer_panel, text="Stream Delay ms").grid(row=1, column=4, sticky=tk.W, padx=(12, 0), pady=(8, 0))
        ttk.Spinbox(
            timer_panel,
            from_=0,
            to=5000,
            increment=25,
            width=7,
            textvariable=self.timer_stream_delay_ms,
        ).grid(row=1, column=5, sticky=tk.W, pady=(8, 0))

        timer_buttons = ttk.Frame(timer_panel)
        timer_buttons.grid(row=2, column=0, columnspan=6, sticky=tk.E, pady=(10, 0))
        ttk.Button(timer_buttons, text="Refresh Status", command=self.refresh_timer_camera_status).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(timer_buttons, text="Capture Still", command=self.capture_photo).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(timer_buttons, text="Apply Camera Settings", command=self.apply_timer_camera_settings).pack(
            side=tk.LEFT
        )

        ocr_panel = ttk.LabelFrame(shell, text="OCR Result", padding=8)
        ocr_panel.pack(fill=tk.X, pady=(10, 0))
        self.ocr_text = tk.Text(ocr_panel, height=7, wrap=tk.WORD)
        self.ocr_text.pack(fill=tk.X, expand=False)

        preview = ttk.Frame(shell, padding=(0, 12, 0, 8))
        preview.pack(fill=tk.BOTH, expand=True)
        self.image_label = ttk.Label(preview, text="Waiting for connection", anchor=tk.CENTER)
        self.image_label.pack(fill=tk.BOTH, expand=True)

        status = ttk.Label(shell, textvariable=self.status_text, anchor=tk.W)
        status.pack(fill=tk.X)

    def connect(self) -> None:
        settings = self._read_settings()
        if settings is None:
            return

        self.stop(join=True)
        if settings.source_kind == "xiao_usb":
            self.preview_photo = None
            self.image_label.configure(
                image="",
                text="USB Type-C still mode: press Capture to request one ESP32 image.",
            )
            self.status_text.set("Checking USB connection...")
            self._ping_usb_still()
            return

        if settings.source_kind == "xiao_tcp":
            port = self._read_tcp_port()
            if port is None:
                return
            self.preview_photo = None
            self.image_label.configure(
                image="",
                text="TCP still mode: press Capture to request one ESP32 image.",
            )
            self.status_text.set("Checking TCP connection...")
            self._ping_tcp_still(settings.source, port)
            return

        if settings.source_kind == "timercamera":
            self._send_control_if_needed(settings)
            self.worker = TimerCameraHttpWorker(settings, self.frame_queue, self.status_queue)
        elif settings.source_kind == "vrc_udp":
            ports = self._read_udp_ports()
            if ports is None:
                return
            esp32_port, _control_port = ports
            self.worker = VrcUdpVideoWorker(
                settings,
                esp32_port,
                self.frame_queue,
                self.status_queue,
            )
        else:
            self._send_control_if_needed(settings)
            self.worker = VideoWorker(settings, self.frame_queue, self.status_queue)

        self.worker.start()
        if settings.source_kind == "timercamera":
            self.refresh_timer_camera_status()
        self.status_text.set("Connecting...")

    def apply_and_reconnect(self) -> None:
        self.connect()

    def capture_photo(self, auto: bool = False) -> None:
        settings = self._read_settings()
        if settings is None:
            return

        if settings.source_kind == "xiao_tcp":
            self._request_tcp_capture(settings, auto=auto)
            return

        if settings.source_kind == "xiao_usb":
            self._request_usb_capture(auto=auto)
            return

        if settings.source_kind == "timercamera":
            self._request_timercamera_capture(settings, auto=auto)
            return

        if self.latest_frame is None:
            messagebox.showinfo("Capture", "No image is available to save. Connect to a camera first.")
            return

        self._finish_captured_image(self.latest_frame.copy(), apply_orientation=False)

    def _finish_captured_image(self, image: Image.Image, apply_orientation: bool = True) -> None:
        if apply_orientation:
            image = self._apply_orientation(image)

        self.latest_frame = image.copy()
        self._display_image(image)

        raw_path, candidates, saved = self._save_capture_files(image)
        names = ", ".join(path.name for path in saved)
        self.status_text.set(f"Capture saved: {names}")

        if self.run_ocr_on_capture.get():
            self._start_ocr(raw_path, candidates)

    def _save_capture_files(self, image: Image.Image) -> tuple[Path, list[Path], list[Path]]:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        raw_path = CAPTURE_DIR / f"{timestamp}_raw.png"
        image.save(raw_path)

        saved = [raw_path]
        candidates = [raw_path]
        ocr_engine = normalize_ocr_engine(self.ocr_engine.get())
        prepare_tesseract_candidates = self.run_ocr_on_capture.get() and ocr_engine == "tesseract"
        if self.save_ocr_images.get() or prepare_tesseract_candidates:
            generated_candidates = []
            for suffix, candidate_image in build_ocr_candidate_images(
                image,
                high_accuracy=self.high_accuracy_ocr.get(),
            ).items():
                candidate_path = CAPTURE_DIR / f"{timestamp}_{suffix}.png"
                candidate_image.save(candidate_path)
                generated_candidates.append(candidate_path)
                if self.save_ocr_images.get():
                    saved.append(candidate_path)
            if ocr_engine == "tesseract":
                candidates = generated_candidates + [raw_path]

        return raw_path, candidates, saved

    def _request_tcp_capture(self, settings: CameraSettings, auto: bool = False) -> None:
        if self.capture_busy:
            if not auto:
                self.status_text.set("TCP capture is running. Wait a moment.")
            return

        port = self._read_tcp_port()
        if port is None:
            return

        self.capture_busy = True
        self.status_text.set("Requesting TCP capture...")

        def task() -> None:
            try:
                image = request_tcp_still_image(settings.source, port)
                self.capture_queue.put(("ok", image, f"TCP capture received: {image.width}x{image.height}"))
            except Exception as exc:
                self.capture_queue.put(("error", None, f"TCP capture failed: {exc}"))

        threading.Thread(target=task, daemon=True).start()

    def _request_usb_capture(self, auto: bool = False) -> None:
        if self.capture_busy:
            if not auto:
                self.status_text.set("USB capture is running. Wait a moment.")
            return

        client = self._get_usb_client()
        if client is None:
            return

        self.capture_busy = True
        self.status_text.set("Requesting USB capture...")

        def task() -> None:
            try:
                image = client.capture()
                self.capture_queue.put(("ok", image, f"USB capture received: {image.width}x{image.height}"))
            except Exception as exc:
                self.capture_queue.put(("error", None, f"USB capture failed: {exc}"))

        threading.Thread(target=task, daemon=True).start()

    def _request_timercamera_capture(self, settings: CameraSettings, auto: bool = False) -> None:
        if self.capture_busy:
            if not auto:
                self.status_text.set("TimerCamera-F capture is running. Wait a moment.")
            return

        self.capture_busy = True
        capture_url = build_timercamera_capture_url(settings.source)
        self.status_text.set("Requesting TimerCamera-F capture...")

        def task() -> None:
            try:
                image = request_http_image(capture_url)
                self.capture_queue.put(
                    ("ok", image, f"TimerCamera-F capture received: {image.width}x{image.height}")
                )
            except Exception as exc:
                self.capture_queue.put(("error", None, f"TimerCamera-F capture failed: {exc}"))

        threading.Thread(target=task, daemon=True).start()

    def start_auto_capture(self) -> None:
        if self.auto_capture_running:
            return
        if self.source_kind.get() not in {"xiao_usb", "xiao_tcp", "timercamera"}:
            messagebox.showinfo(
                "Auto Capture",
                "Auto capture is available only for TimerCamera-F, XIAO USB Type-C, or XIAO TCP still mode.",
            )
            return
        interval_ms = self._read_auto_interval_ms()
        if interval_ms is None:
            return

        self.auto_capture_running = True
        self.status_text.set(f"Auto capture started: {interval_ms / 1000:g} second interval")
        self._auto_capture_tick()

    def stop_auto_capture(self) -> None:
        self.auto_capture_running = False
        if self.auto_capture_after_id is not None:
            try:
                self.root.after_cancel(self.auto_capture_after_id)
            except tk.TclError:
                pass
            self.auto_capture_after_id = None
        self.status_text.set("Auto capture stopped.")

    def _auto_capture_tick(self) -> None:
        if not self.auto_capture_running:
            return

        interval_ms = self._read_auto_interval_ms(show_error=False) or 500
        self.capture_photo(auto=True)
        self.auto_capture_after_id = self.root.after(interval_ms, self._auto_capture_tick)

    def ocr_existing_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Select image for OCR",
            filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        source = Path(path)
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        raw_path = CAPTURE_DIR / f"{timestamp}_{source.stem}_raw.png"
        image = Image.open(source).convert("RGB")
        image.save(raw_path)
        candidates = [raw_path]
        ocr_engine = normalize_ocr_engine(self.ocr_engine.get())
        if self.save_ocr_images.get() or ocr_engine == "tesseract":
            generated_candidates = []
            for suffix, candidate_image in build_ocr_candidate_images(
                image,
                high_accuracy=self.high_accuracy_ocr.get(),
            ).items():
                candidate_path = CAPTURE_DIR / f"{timestamp}_{source.stem}_{suffix}.png"
                candidate_image.save(candidate_path)
                generated_candidates.append(candidate_path)
            if ocr_engine == "tesseract":
                candidates = generated_candidates + [raw_path]
        self.status_text.set(f"OCR image ready: {raw_path.name}")
        self._start_ocr(raw_path, candidates)

    def _start_ocr(self, raw_path: Path, candidate_paths: list[Path]) -> None:
        language = self.ocr_language.get().strip() or DEFAULT_OCR_LANGUAGE
        engine = normalize_ocr_engine(self.ocr_engine.get())
        high_accuracy = self.high_accuracy_ocr.get()
        job = (raw_path, candidate_paths, language, engine, high_accuracy)
        if self.ocr_busy:
            self.pending_ocr = job
            self.status_text.set("OCR is running. The latest pending image will be processed next.")
            return

        self._launch_ocr_job(job)

    def _launch_ocr_job(self, job: tuple[Path, list[Path], str, str, bool]) -> None:
        raw_path, candidate_paths, language, engine, high_accuracy = job
        self.ocr_busy = True
        self.ocr_job_id += 1
        job_id = self.ocr_job_id
        self.status_text.set(f"{engine} OCR running...")

        def task() -> None:
            try:
                saved = save_ocr_outputs(
                    raw_path,
                    candidate_paths,
                    language,
                    high_accuracy=high_accuracy,
                    engine=engine,
                )
                menu_summary = (
                    f"menu {saved.menu_match_count}/{saved.menu_item_count}"
                    if saved.menu_item_count
                    else "menu database not loaded"
                )
                message = (
                    f"OCR complete: {saved.result_path.name}, {saved.transcript_path.name} "
                    f"({engine}, conf {saved.confidence:.1f}, image {saved.best_path.name}, {menu_summary})"
                )
                self.ocr_queue.put((job_id, "ok", message, saved.corrected_text))
            except Exception as exc:
                self.ocr_queue.put((job_id, "error", f"OCR failed: {exc}", ""))

        threading.Thread(target=task, daemon=True).start()

    def stop(self, join: bool = False) -> None:
        if self.auto_capture_running:
            self.stop_auto_capture()

        if self.usb_client is not None:
            self.usb_client.close()
            self.usb_client = None

        if self.worker is None:
            if not join:
                self.status_text.set("Stopped.")
            return

        self.worker.stop()
        if join:
            self.worker.join(timeout=2.0)
        self.worker = None
        self.status_text.set("Stop requested.")

    def _read_settings(self) -> CameraSettings | None:
        try:
            width = int(self.width.get())
            height = int(self.height.get())
            fps = float(self.fps.get())
        except ValueError:
            messagebox.showerror("Input Error", "Width, height, and FPS must be numeric.")
            return None

        if width <= 0 or height <= 0:
            messagebox.showerror("Input Error", "Width and height must be at least 1.")
            return None
        if fps < 0:
            messagebox.showerror("Input Error", "FPS must be 0 or higher. Use 0 for unlimited.")
            return None

        source_kind = self.source_kind.get()
        if source_kind == "timercamera":
            source_input = self._read_timercamera_source_input()
            try:
                source = build_timercamera_stream_url(source_input)
            except ValueError as exc:
                messagebox.showerror("Input Error", str(exc))
                return None
        elif source_kind == "xiao_usb":
            source = self.serial_port.get().strip()
            if not source:
                messagebox.showerror("Input Error", "Enter a COM port for XIAO USB Type-C mode.")
                return None
            try:
                baud = int(self.serial_baud.get())
            except ValueError:
                messagebox.showerror("Input Error", "USB baud must be an integer.")
                return None
            if baud <= 0:
                messagebox.showerror("Input Error", "USB baud must be at least 1.")
                return None
        elif source_kind in {"xiao_tcp", "vrc_udp"}:
            source = self.stream_url.get().strip()
            if not source:
                messagebox.showerror("Input Error", "Enter the ESP32 IP address for XIAO mode.")
                return None
            if source.startswith(("http://", "https://")):
                messagebox.showerror("Input Error", "Enter only the ESP32 IP address for XIAO mode, not a URL.")
                return None
        elif source_kind == "usb":
            try:
                int(self.usb_index.get())
            except ValueError:
                messagebox.showerror("Input Error", "USB index must be an integer.")
                return None
            source = self.usb_index.get()
        else:
            source = self.stream_url.get().strip()
            if not source.startswith(("http://", "https://")):
                messagebox.showerror("Input Error", "Stream URL must start with http:// or https://.")
                return None

        return CameraSettings(
            source_kind=source_kind,
            source=source,
            width=width,
            height=height,
            fps=fps,
            resize_display=self.resize_display.get(),
        )

    def _read_udp_ports(self) -> tuple[int, int] | None:
        try:
            listen_port = int(self.udp_listen_port.get())
            control_port = int(self.udp_control_port.get())
        except ValueError:
            messagebox.showerror("Input Error", "UDP ports must be integers.")
            return None

        if not (1 <= listen_port <= 65535 and 1 <= control_port <= 65535):
            messagebox.showerror("Input Error", "UDP ports must be in the range 1 to 65535.")
            return None

        return listen_port, control_port

    def _read_tcp_port(self) -> int | None:
        try:
            port = int(self.tcp_still_port.get())
        except ValueError:
            messagebox.showerror("Input Error", "TCP port must be an integer.")
            return None

        if not (1 <= port <= 65535):
            messagebox.showerror("Input Error", "TCP port must be in the range 1 to 65535.")
            return None
        return port

    def _read_serial_baud(self) -> int | None:
        try:
            baud = int(self.serial_baud.get())
        except ValueError:
            messagebox.showerror("Input Error", "USB baud must be an integer.")
            return None

        if baud <= 0:
            messagebox.showerror("Input Error", "USB baud must be at least 1.")
            return None
        return baud

    def _read_auto_interval_ms(self, show_error: bool = True) -> int | None:
        try:
            seconds = float(self.auto_interval.get())
        except ValueError:
            if show_error:
                messagebox.showerror("Input Error", "Auto capture interval seconds must be numeric.")
            return None

        if seconds < 0.2:
            if show_error:
                messagebox.showerror("Input Error", "Auto capture interval must be at least 0.2 seconds.")
            return None
        return int(seconds * 1000)

    def _read_timercamera_source_input(self) -> str:
        return self.stream_url.get().strip()

    def _read_timer_camera_controls(self) -> tuple[str, int, int] | None:
        frame_label = self.timer_frame_size.get()
        frame_value = TIMER_CAMERA_FRAME_SIZE_VALUES.get(frame_label)
        if frame_value is None:
            messagebox.showerror("Input Error", "Select a valid TimerCamera-F frame size.")
            return None

        try:
            jpeg_quality = int(self.timer_jpeg_quality.get())
            stream_delay_ms = int(self.timer_stream_delay_ms.get())
        except ValueError:
            messagebox.showerror("Input Error", "JPEG quality and stream delay must be integers.")
            return None

        if not (4 <= jpeg_quality <= 63):
            messagebox.showerror("Input Error", "JPEG quality must be in the range 4 to 63.")
            return None
        if not (0 <= stream_delay_ms <= 5000):
            messagebox.showerror("Input Error", "Stream delay must be in the range 0 to 5000 ms.")
            return None
        return frame_value, jpeg_quality, stream_delay_ms

    def _current_timercamera_source(self) -> str | None:
        try:
            return build_timercamera_stream_url(self._read_timercamera_source_input())
        except ValueError as exc:
            messagebox.showerror("Input Error", str(exc))
            return None

    def refresh_timer_camera_status(self) -> None:
        source = self._current_timercamera_source()
        if source is None:
            return

        status_url = build_timercamera_status_url(source)
        self.status_text.set("Reading TimerCamera-F status...")

        def task() -> None:
            try:
                data = request_json(status_url)
                self.device_status_queue.put(("ok", data, "TimerCamera-F status updated."))
            except Exception as exc:
                self.device_status_queue.put(("error", None, f"TimerCamera-F status failed: {exc}"))

        threading.Thread(target=task, daemon=True).start()

    def apply_timer_camera_settings(self) -> None:
        settings = self._read_settings()
        if settings is None:
            return
        if settings.source_kind != "timercamera":
            messagebox.showinfo("TimerCamera-F Control", "Select TimerCamera-F HTTP mode first.")
            return

        self._send_control_if_needed(settings, force=True)

    def _ping_tcp_still(self, host: str, port: int) -> None:
        def task() -> None:
            try:
                response = ping_tcp_still(host, port)
                if response == "OK PONG":
                    self.status_queue.put(f"TCP connection OK: {host}:{port} / press Capture to receive an image")
                else:
                    self.status_queue.put(f"Unexpected TCP response: {response}")
            except Exception as exc:
                self.status_queue.put(f"TCP connection check failed: {exc}")

        threading.Thread(target=task, daemon=True).start()

    def _get_usb_client(self) -> UsbStillClient | None:
        port = self.serial_port.get().strip()
        if not port:
            messagebox.showerror("Input Error", "Enter the XIAO COM port.")
            return None

        baud = self._read_serial_baud()
        if baud is None:
            return None

        if self.usb_client is None or self.usb_client.port != port or self.usb_client.baudrate != baud:
            if self.usb_client is not None:
                self.usb_client.close()
            self.usb_client = UsbStillClient(port, baud)
        return self.usb_client

    def _ping_usb_still(self) -> None:
        client = self._get_usb_client()
        if client is None:
            return

        def task() -> None:
            try:
                response = client.ping()
                if response == "OK PONG":
                    self.status_queue.put(
                        f"USB connection OK: {client.port} / press Capture to receive an image"
                    )
                else:
                    self.status_queue.put(f"Unexpected USB response: {response}")
            except Exception as exc:
                self.status_queue.put(f"USB connection check failed: {exc}")

        threading.Thread(target=task, daemon=True).start()

    def _send_control_if_needed(self, settings: CameraSettings, force: bool = False) -> None:
        if settings.source_kind == "timercamera":
            if not force and not self.send_control.get():
                return

            controls = self._read_timer_camera_controls()
            if controls is None:
                return
            frame_size, jpeg_quality, stream_delay_ms = controls
            control_url = self.control_url.get().strip() or build_timercamera_control_url(settings.source)

            def task() -> None:
                url = with_query(
                    control_url,
                    framesize=frame_size,
                    quality=jpeg_quality,
                    delay_ms=stream_delay_ms,
                )
                try:
                    with urlopen(url, timeout=3.0) as response:
                        status = getattr(response, "status", response.getcode())
                        data = json.loads(response.read(256 * 1024).decode("utf-8"))
                    if 200 <= status < 300 and isinstance(data, dict):
                        self.device_status_queue.put(
                            (
                                "ok",
                                data,
                                f"TimerCamera-F settings applied: {frame_size}, q{jpeg_quality}, {stream_delay_ms}ms",
                            )
                        )
                    else:
                        self.status_queue.put(f"TimerCamera-F control failed: HTTP {status}")
                except HTTPError as exc:
                    self.status_queue.put(f"TimerCamera-F control failed: HTTP {exc.code}")
                except (json.JSONDecodeError, URLError, OSError, TimeoutError) as exc:
                    self.status_queue.put(f"Could not connect to TimerCamera-F control endpoint: {exc}")

            threading.Thread(target=task, daemon=True).start()
            return

        if settings.source_kind != "esp32" or not self.send_control.get():
            return

        control_url = self.control_url.get().strip() or derive_control_url(settings.source)
        selected = nearest_esp32_framesize(settings.width, settings.height)
        name, selected_width, selected_height, value = selected

        def task() -> None:
            url = with_query(control_url, var="framesize", val=value)
            try:
                with urlopen(url, timeout=2.0) as response:
                    status = getattr(response, "status", response.getcode())
                if 200 <= status < 300:
                    self.status_queue.put(
                        f"Sent ESP32 frame size: {name} ({selected_width}x{selected_height})"
                    )
                else:
                    self.status_queue.put(f"ESP32 control failed: HTTP {status}")
            except HTTPError as exc:
                self.status_queue.put(f"ESP32 control failed: HTTP {exc.code}")
            except (URLError, OSError, TimeoutError) as exc:
                self.status_queue.put(f"Could not connect to ESP32 control endpoint: {exc}")

        threading.Thread(target=task, daemon=True).start()

    def _sync_timer_camera_controls_from_status(self, data: dict[str, object]) -> None:
        frame_size = str(data.get("frame_size") or "")
        if frame_size:
            for label, value in TIMER_CAMERA_FRAME_SIZES:
                if value == frame_size:
                    self.timer_frame_size.set(label)
                    break

        jpeg_quality = data.get("jpeg_quality")
        if jpeg_quality is not None:
            self.timer_jpeg_quality.set(str(jpeg_quality))

        stream_delay_ms = data.get("stream_delay_ms")
        if stream_delay_ms is not None:
            self.timer_stream_delay_ms.set(str(stream_delay_ms))

    def _poll_queues(self) -> None:
        while True:
            try:
                self.status_text.set(self.status_queue.get_nowait())
            except queue.Empty:
                break

        while True:
            try:
                kind, data, message = self.device_status_queue.get_nowait()
            except queue.Empty:
                break

            self.status_text.set(message)
            if kind == "ok" and data is not None:
                self.device_status_text.set(format_timercamera_status(data))
                self._sync_timer_camera_controls_from_status(data)
            else:
                self.device_status_text.set(message)

        while True:
            try:
                job_id, kind, message, text = self.ocr_queue.get_nowait()
            except queue.Empty:
                break
            if job_id != self.ocr_job_id:
                continue

            self.ocr_busy = False
            if self.pending_ocr is not None:
                next_job = self.pending_ocr
                self.pending_ocr = None
                self._launch_ocr_job(next_job)
                continue

            self.status_text.set(message)
            if kind == "ok":
                self.ocr_text.delete("1.0", tk.END)
                self.ocr_text.insert(tk.END, text or "(no text detected)")
            else:
                self.ocr_text.delete("1.0", tk.END)
                self.ocr_text.insert(tk.END, message)

        while True:
            try:
                kind, image, message = self.capture_queue.get_nowait()
            except queue.Empty:
                break
            self.capture_busy = False
            self.status_text.set(message)
            if kind == "ok" and image is not None:
                self._finish_captured_image(image)

        latest: Image.Image | None = None
        while True:
            try:
                latest = self.frame_queue.get_nowait()
            except queue.Empty:
                break

        if latest is not None:
            oriented = self._apply_orientation(latest)
            self.latest_frame = oriented.copy()
            self._display_image(oriented)

        self.root.after(15, self._poll_queues)

    def _apply_orientation(self, image: Image.Image) -> Image.Image:
        oriented = image
        if self.flip_horizontal.get():
            oriented = ImageOps.mirror(oriented)
        if self.flip_vertical.get():
            oriented = ImageOps.flip(oriented)
        return oriented

    def _display_image(self, image: Image.Image) -> None:
        preview = self._fit_preview(image)
        self.preview_photo = ImageTk.PhotoImage(preview)
        self.image_label.configure(image=self.preview_photo, text="")

    def _fit_preview(self, image: Image.Image) -> Image.Image:
        preview = image.copy()
        max_width = max(320, self.image_label.winfo_width() - 4)
        max_height = max(240, self.image_label.winfo_height() - 4)
        if max_width <= 340 or max_height <= 260:
            max_width, max_height = 920, 520
        preview.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        return preview

    def _on_close(self) -> None:
        self.stop(join=True)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.1)
    except tk.TclError:
        pass
    CameraViewerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
