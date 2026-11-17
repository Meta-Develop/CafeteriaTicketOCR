"""Local Japanese RapidOCR and preserved capture results; no startup inference."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import math
import re
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MENU_DATABASE_PATH = REPO_ROOT / "data" / "menu.json"
CAPTURE_DIR = REPO_ROOT / "runtime" / "captures"
DEFAULT_OCR_ENGINE = "rapidocr"
RAPIDOCR_MODELS = {
    "Det": "ch_PP-OCRv4_det_mobile.onnx",
    "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "Rec": "japan_PP-OCRv4_rec_mobile.onnx",
}
_RAPIDOCR_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()


def rapidocr_model_paths() -> dict[str, Path]:
    spec = importlib.util.find_spec("rapidocr")
    if spec is None or spec.origin is None:
        raise RuntimeError("日本語認識ライブラリがありません。セットアップが必要です。")
    model_dir = Path(spec.origin).parent / "models"
    return {task: model_dir / name for task, name in RAPIDOCR_MODELS.items()}


def ocr_status(menu_path: Path = MENU_DATABASE_PATH) -> dict:
    try:
        from ocr_menu import load_menu_items
        count = len(load_menu_items(menu_path))
        if not count:
            raise RuntimeError("メニューデータがありません。管理者に確認してください。")
        if importlib.util.find_spec("onnxruntime") is None:
            raise RuntimeError("日本語認識ライブラリがありません。セットアップが必要です。")
        if any(not path.is_file() for path in rapidocr_model_paths().values()):
            raise RuntimeError("日本語認識モデルがありません。セットアップが必要です。")
        return {"ready": True, "menu_count": count, "error": None}
    except RuntimeError as exc:
        return {"ready": False, "menu_count": locals().get("count", 0), "error": str(exc)}
    except (ImportError, SyntaxError, OSError, ValueError, TypeError):
        return {"ready": False, "menu_count": 0, "error": "メニューデータを読み込めません。管理者に確認してください。"}


def ocr_unavailable_reason(engine: str = DEFAULT_OCR_ENGINE) -> str | None:
    if engine != DEFAULT_OCR_ENGINE:
        return "このアプリは日本語 RapidOCR のみ対応しています。"
    return ocr_status()["error"]


def get_rapidocr_engine(*, allow_download: bool = False):
    global _RAPIDOCR_ENGINE
    with _ENGINE_LOCK:
        if _RAPIDOCR_ENGINE is None:
            # Check local files before importing/constructing a downloader-capable engine.
            paths = rapidocr_model_paths()
            if not allow_download and any(not path.is_file() for path in paths.values()):
                raise RuntimeError("日本語認識モデルがありません。セットアップが必要です。")
            with contextlib.redirect_stdout(sys.stderr):
                from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
                params = {
                    "Global.log_level": "warning",
                    "Global.use_cls": False,
                    "EngineConfig.onnxruntime.intra_op_num_threads": 1,
                    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                    "Rec.lang_type": LangRec.JAPAN,
                    "Rec.model_type": ModelType.MOBILE,
                    "Rec.ocr_version": OCRVersion.PPOCRV4,
                }
                if not allow_download:
                    params.update({f"{task}.model_path": str(path) for task, path in paths.items()})
                _RAPIDOCR_ENGINE = RapidOCR(params=params)
        return _RAPIDOCR_ENGINE


def transcribe_ocr_text(text: str) -> str:
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"(?<=[\u3040-\u30ff\u3400-\u9fff])\s+(?=[\u3040-\u30ff\u3400-\u9fff])", "", line.strip())
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def recognize_image(raw_path, engine=None, menu_path: Path = MENU_DATABASE_PATH) -> dict:
    from ocr_menu import select_best_menu_item
    with _INFERENCE_LOCK, contextlib.redirect_stdout(sys.stderr):
        output = (engine or get_rapidocr_engine())(str(raw_path) if isinstance(raw_path, Path) else raw_path, use_cls=False)
    texts = getattr(output, "txts", None)
    scores = getattr(output, "scores", None)
    text = transcribe_ocr_text("\n".join(str(value) for value in texts)) if texts is not None else ""
    values = [float(value) * 100.0 for value in scores if math.isfinite(float(value))] if scores is not None else []
    selection = select_best_menu_item(text, menu_path)
    item = selection.selected_item
    return {
        "status": "matched" if item is not None else "no_match",
        "name": item.name if item else None,
        "price": item.price if item else None,
        "score": selection.score if item else None,
        "exact_name": bool(item and selection.name_score >= 1),
        "text": text,
        "confidence": sum(values) / len(values) if values else None,
        "message": "メニューが見つかりました。" if item else "該当するメニューが見つかりません。券全体を明るく写して、もう一度お試しください。",
    }


def capture_and_recognize(frame, capture_id: str, capture_dir: Path = CAPTURE_DIR, recognize=recognize_image) -> dict:
    """Keep the original full-resolution PNG and all outcomes, including errors."""
    import cv2
    started = time.monotonic()
    result = {"capture_id": capture_id, "status": "error", "name": None, "price": None,
              "score": None, "text": "", "confidence": None, "elapsed_ms": 0,
              "message": "認識できませんでした。もう一度お試しください。"}
    raw_path = capture_dir / f"{capture_id}_raw.png"
    if any((capture_dir / f"{capture_id}{suffix}").exists()
           for suffix in ("_raw.png", "_ocr_result.txt", "_result.json")):
        raise FileExistsError("capture id already exists")
    try:
        capture_dir.mkdir(parents=True, exist_ok=True)
        ok, encoded = cv2.imencode(".png", frame)
        if not ok:
            raise OSError("capture write failed")
        with raw_path.open("xb") as output:
            output.write(encoded.tobytes())
        result.update(recognize(raw_path))
    except Exception as exc:
        # Exception messages may contain private OCR text or local data.
        print(f"recognition failed: {type(exc).__name__}", file=sys.stderr)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    try:
        with raw_path.with_name(f"{capture_id}_ocr_result.txt").open("x", encoding="utf-8") as output:
            output.write((result["name"] or "") + ("\n" if result["name"] else ""))
    except OSError:
        result.update(status="error", message="撮影結果を保存できません。保存先の空き容量と権限を確認してください。")
    try:
        with raw_path.with_name(f"{capture_id}_result.json").open("x", encoding="utf-8") as output:
            output.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    except (OSError, ValueError):
        result.update(status="error", message="撮影結果を保存できません。保存先の空き容量と権限を確認してください。")
    return result
