"""Validate explicitly selected local assets before loading native inference libraries.

The fixed layout revision prevents an ambient cache or changed weight file from
silently becoming a different extraction engine. OCR requires explicit executable
and language data paths, independent of PATH and TESSDATA_PREFIX.
The selected tessdata directory must contain osd.traineddata for orientation detection.
It must also contain configs/tsv. Docling asks Tesseract for its tsv output configuration,
which Tesseract reads from that directory, so those bytes change recognition like model data.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path

MODEL_REPOSITORY = "docling-project/docling-layout-heron-onnx"
MODEL_REVISION = "40bde044036bb181c130ddf6c51792187268748f"
MODEL_FILES = {
    "config.json": "c6e67b6cdf64fa245779d0409bb994aa96e8c1790e15ec07a65843628e232180",
    "preprocessor_config.json": "54d086cf0d7d371f7fac36e5d3a0dae31e211298affc816106cc376506799d58",
    "model.onnx": "59c81a3a2923042d85034ffc487f8f47e4854117e879aef89b2b9f728fb4922a",
}
INTEGRATION_REVISION = "docling-onnx-cpu-pil-v3"


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class LocalDoclingConfig:
    artifacts_path: Path
    ocr: bool = False
    tesseract_cmd: Path | None = None
    tessdata_path: Path | None = None
    languages: tuple[str, ...] = ("eng",)
    threads: int = 4
    dependency_lock: Path | None = None

    def __post_init__(self):
        if (
            not isinstance(self.artifacts_path, Path)
            or type(self.ocr) is not bool
            or type(self.threads) is not int
            or not isinstance(self.languages, tuple)
            or any(not isinstance(lang, str) for lang in self.languages)
            or any(
                value is not None and not isinstance(value, Path)
                for value in (self.tesseract_cmd, self.tessdata_path, self.dependency_lock)
            )
        ):
            raise ValueError("Invalid local profile configuration.")

    def wire(self) -> dict:
        value = asdict(self)
        for key in ("artifacts_path", "tesseract_cmd", "tessdata_path", "dependency_lock"):
            value[key] = str(value[key]) if value[key] is not None else None
        value["languages"] = list(self.languages)
        return value

    @classmethod
    def from_wire(cls, value: dict):
        data = dict(value)
        for key in ("artifacts_path", "tesseract_cmd", "tessdata_path", "dependency_lock"):
            if data.get(key) is not None:
                data[key] = Path(data[key])
        if "languages" in data:
            if not isinstance(data["languages"], list):
                raise ValueError("Languages require an array.")
            data["languages"] = tuple(data["languages"])
        if type(data.get("ocr", False)) is not bool or type(data.get("threads", 4)) is not int:
            raise ValueError("Invalid local profile configuration.")
        return cls(**data)

    def validate_assets(self) -> dict[str, str]:
        try:
            if not self.artifacts_path.is_absolute() or not 1 <= self.threads <= 16:
                raise ValueError
            model = self.artifacts_path / MODEL_REPOSITORY.replace("/", "--")
            hashes = {name: file_digest(model / name) for name in MODEL_FILES}
            if hashes != MODEL_FILES:
                raise ValueError
            if self.ocr:
                if (
                    self.tesseract_cmd is None
                    or not self.tesseract_cmd.is_absolute()
                    or self.tessdata_path is None
                    or not self.tessdata_path.is_absolute()
                    or not self.languages
                    or "auto" in self.languages
                    or any(not re.fullmatch(r"[A-Za-z0-9_]+", lang) for lang in self.languages)
                ):
                    raise ValueError
                hashes["tesseract"] = file_digest(self.tesseract_cmd)
                for lang in sorted({*self.languages, "osd"}):
                    hashes[f"{lang}.traineddata"] = file_digest(
                        self.tessdata_path / f"{lang}.traineddata"
                    )
                hashes["configs/tsv"] = file_digest(self.tessdata_path / "configs" / "tsv")
            if self.dependency_lock is not None:
                if not self.dependency_lock.is_absolute():
                    raise ValueError
                hashes["dependency_lock"] = file_digest(self.dependency_lock)
            return hashes
        except (OSError, ValueError):
            raise ValueError("Local Docling assets are missing, changed, or invalid.") from None
