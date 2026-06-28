from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


VISION_OCR_SWIFT = r'''
import Foundation
import ImageIO
import Vision

if CommandLine.arguments.count < 2 {
    fputs("usage: vision_ocr IMAGE\n", stderr)
    exit(2)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fputs("cannot load image: \(CommandLine.arguments[1])\n", stderr)
    exit(1)
}

let request = VNRecognizeTextRequest { request, error in
    if let error = error {
        fputs("ocr error: \(error)\n", stderr)
        exit(1)
    }
    let observations = (request.results as? [VNRecognizedTextObservation]) ?? []
    let sorted = observations.sorted {
        if abs($0.boundingBox.midY - $1.boundingBox.midY) > 0.01 {
            return $0.boundingBox.midY > $1.boundingBox.midY
        }
        return $0.boundingBox.minX < $1.boundingBox.minX
    }
    for observation in sorted {
        if let text = observation.topCandidates(1).first {
            print(text.string)
        }
    }
}

request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
if #available(macOS 11.0, *) {
    request.recognitionLanguages = ["zh-Hans", "en-US"]
}

let handler = VNImageRequestHandler(cgImage: image, options: [:])
do {
    try handler.perform([request])
} catch {
    fputs("ocr failed: \(error)\n", stderr)
    exit(1)
}
'''


def ocr_pdf_with_macos_vision(pdf_path: Path, config: dict[str, Any]) -> str:
    ocr_config = config.get("ocr", {})
    if not ocr_config.get("enabled", False):
        return ""
    if ocr_config.get("engine") != "macos_vision":
        raise RuntimeError(f"Unsupported OCR engine: {ocr_config.get('engine')}")

    pdftoppm = shutil.which("pdftoppm")
    swiftc = shutil.which("swiftc")
    if not pdftoppm or not swiftc:
        missing = ", ".join(name for name, path in (("pdftoppm", pdftoppm), ("swiftc", swiftc)) if not path)
        raise RuntimeError(f"Image-only PDF requires OCR dependencies: {missing}")

    with tempfile.TemporaryDirectory(prefix="a_share_t1_ocr_") as temp_name:
        temp_dir = Path(temp_name)
        ocr_bin = _compile_vision_ocr(swiftc, temp_dir)
        images = _render_pdf(pdftoppm, pdf_path, temp_dir, int(ocr_config.get("dpi", 120)))
        page_texts = [_ocr_image(ocr_bin, image) for image in images]
        parts = [f"\n--- PAGE {index:02d} ---\n{text}" for index, text in enumerate(page_texts, start=1)]

        detail_start = _first_page_containing(page_texts, ("每支股票", "逐股详细"))
        sector_start = _first_page_containing(page_texts, ("热门板块", "资金轮动"))
        crop_count = int(ocr_config.get("detail_column_crops", 0))
        if detail_start is not None:
            crop_end = sector_start if sector_start is not None and sector_start > detail_start else len(images)
            if crop_count > 1:
                parts.extend(_ocr_detail_crops(ocr_bin, images[detail_start + 1 : crop_end], crop_count, temp_dir))
        elif crop_count > 1 and _stock_code_count("\n".join(page_texts)) < 3:
            parts.extend(_ocr_detail_crops(ocr_bin, images, crop_count, temp_dir))
        return "\n".join(parts).strip()


def _compile_vision_ocr(swiftc: str, temp_dir: Path) -> Path:
    source = temp_dir / "vision_ocr.swift"
    binary = temp_dir / "vision_ocr"
    source.write_text(VISION_OCR_SWIFT, encoding="utf-8")
    env = os.environ.copy()
    env["CLANG_MODULE_CACHE_PATH"] = str(temp_dir / "clang-cache")
    subprocess.run([swiftc, str(source), "-o", str(binary)], check=True, capture_output=True, text=True, env=env)
    return binary


def _render_pdf(pdftoppm: str, pdf_path: Path, temp_dir: Path, dpi: int) -> list[Path]:
    prefix = temp_dir / "page"
    subprocess.run([pdftoppm, "-r", str(dpi), "-png", str(pdf_path), str(prefix)], check=True, capture_output=True, text=True)
    return sorted(temp_dir.glob("page-*.png"))


def _ocr_image(ocr_bin: Path, image_path: Path) -> str:
    result = subprocess.run([str(ocr_bin), str(image_path)], capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _ocr_detail_crops(ocr_bin: Path, images: list[Path], crop_count: int, temp_dir: Path) -> list[str]:
    from PIL import Image

    parts: list[str] = []
    crop_names = ("left", "mid", "right", "col4", "col5")
    for image in images:
        with Image.open(image) as page:
            width, height = page.size
            for index in range(crop_count):
                crop_path = temp_dir / f"{image.stem}-{crop_names[index] if index < len(crop_names) else index + 1}.png"
                crop = page.crop((int(width * index / crop_count), 0, int(width * (index + 1) / crop_count), height))
                crop.save(crop_path)
                text = _ocr_image(ocr_bin, crop_path)
                parts.append(f"\n--- CROP {crop_path.stem} ---\n{text}")
    return parts


def _first_page_containing(page_texts: list[str], tokens: tuple[str, ...]) -> int | None:
    for index, text in enumerate(page_texts):
        if any(token in text.replace(" ", "") for token in tokens):
            return index
    return None


def _stock_code_count(text: str) -> int:
    return len(re.findall(r"(?:00|30|60|68|43|83|87)\d{4}", text))
