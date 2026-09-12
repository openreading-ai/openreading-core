"""Initialize only the selected Docling stages while preserving its PDF execution.

Docling 2.126.0 imports torch during CPU device selection and disabled table-plugin
loading. Explicit initialization avoids both paths without patching global factories.
Transformers 5 preprocessing uses its explicit PIL implementation with NumPy tensors.
The generic AutoImageProcessor instead requires Torchvision.
The upstream standard pipeline still owns threading, layout postprocessing, and reading order.
Assembly preserves wrapped hyphens because removing them destroys searchable compound words.
For example, third-party must remain searchable as party without rewriting retained quotations.
Ordinary line breaks become spaces; breaks after hyphens remain visible in the stored text.
Upstream typography normalization still handles ligatures and quotation marks.
All native imports occur inside create_converter when conversion starts.
"""

from __future__ import annotations

import importlib.metadata

from openreading.adapters.docling_local.config import LocalDoclingConfig


def create_converter(config: LocalDoclingConfig):
    config.validate_assets()
    if importlib.metadata.version("docling-slim") != "2.126.0":
        raise ValueError("The local pipeline requires its tested Docling version.")
    from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.object_detection_engine_options import (
        OnnxRuntimeObjectDetectionEngineOptions,
    )
    from docling.datamodel.pipeline_options import (
        LayoutObjectDetectionOptions,
        LayoutPostprocessorOptions,
        PdfPipelineOptions,
        TesseractCliOcrOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.models.inference_engines.object_detection.onnxruntime_engine import (
        OnnxRuntimeObjectDetectionEngine,
    )
    from docling.models.stages.heading_hierarchy.heading_hierarchy_model import (
        HeadingHierarchyModel,
    )
    from docling.models.stages.layout.layout_object_detection_model import (
        LayoutObjectDetectionModel,
    )
    from docling.models.stages.layout.layout_postprocessing_model import LayoutPostprocessingModel
    from docling.models.stages.ocr.tesseract_ocr_cli_model import TesseractOcrCliModel
    from docling.models.stages.page_assemble.page_assemble_model import (
        PageAssembleModel,
        PageAssembleOptions,
    )
    from docling.models.stages.page_preprocessing.page_preprocessing_model import (
        PagePreprocessingModel,
        PagePreprocessingOptions,
    )
    from docling.models.stages.reading_order.readingorder_model import (
        ReadingOrderModel,
        ReadingOrderOptions,
    )
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

    class CpuEngine(OnnxRuntimeObjectDetectionEngine):
        def _load_preprocessor(self, model_folder):
            # AutoImageProcessor selects a Torchvision backend in Transformers 5.
            # This pinned layout uses the upstream PIL implementation with NumPy tensors.
            from transformers.models.rt_detr.image_processing_pil_rt_detr import (
                RTDetrImageProcessorPil,
            )

            # A directory lookup can prefer an unhashed processor_config.json sibling.
            return RTDetrImageProcessorPil.from_pretrained(
                str(model_folder / "preprocessor_config.json"), local_files_only=True
            )

        def _resolve_providers(self):
            return ["CPUExecutionProvider"]

    class CpuLayout(LayoutObjectDetectionModel):
        def __init__(self, options, artifacts_path, accelerator_options):
            self.options = options
            self.engine = CpuEngine(
                options=options.engine_options,
                model_config=options.model_spec.get_engine_config(
                    options.engine_options.engine_type
                ),
                accelerator_options=accelerator_options,
                artifacts_path=artifacts_path,
            )
            self.engine.initialize()
            self._label_map = self._build_label_map()
            self._unmapped_label_ids = set()

    class LocalTesseract(TesseractOcrCliModel):
        def _set_languages_and_prefix(self):
            # Setup selects explicit language files; ambient language discovery is irrelevant.
            self._tesseract_languages = list(config.languages)
            self._script_prefix = ""

        def _perform_osd(self, filename):
            import io
            import subprocess

            import pandas as pd

            # Upstream orientation detection omits the configured tessdata directory.
            result = subprocess.run(
                [
                    self._safe_tesseract_cmd,
                    "--tessdata-dir",
                    str(config.tessdata_path),
                    "--psm",
                    "0",
                    "-l",
                    "osd",
                    self._sanitize_filename(filename),
                    "stdout",
                ],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                check=True,
                shell=False,
            )
            return pd.read_csv(
                io.StringIO(result.stdout.decode("utf-8")),
                sep=":",
                header=None,
                names=["key", "value"],
            )

    class DisabledStage:
        def __call__(self, conv_res, pages):
            return pages

    class EvidenceAssembly(PageAssembleModel):
        def sanitize_text(self, lines):
            # One input line bypasses upstream's irreversible word joining, while retaining
            # its typography normalization. Search adds joined forms without changing evidence.
            text = "".join(
                line + ("\n" if line.endswith(("-", "\u00ad", "\u2010")) else " ")
                for line in lines[:-1]
            )
            if lines:
                text += lines[-1]
            return super().sanitize_text([text])

    class LocalPdfPipeline(StandardPdfPipeline):
        def _init_models(self):
            opts = self.pipeline_options
            self.keep_images = False
            self.keep_backend = False
            self.preprocessing_model = PagePreprocessingModel(
                options=PagePreprocessingOptions(images_scale=1.0)
            )
            if config.ocr:
                assert isinstance(opts.ocr_options, TesseractCliOcrOptions)
                self.ocr_model = LocalTesseract(
                    enabled=True,
                    artifacts_path=self.artifacts_path,
                    options=opts.ocr_options,
                    accelerator_options=opts.accelerator_options,
                )
            else:
                self.ocr_model = DisabledStage()
            self.layout_model = CpuLayout(
                opts.layout_options, self.artifacts_path, opts.accelerator_options
            )
            self.layout_postprocessing_model = LayoutPostprocessingModel(
                options=LayoutPostprocessorOptions()
            )
            self.table_model = DisabledStage()
            self.assemble_model = EvidenceAssembly(options=PageAssembleOptions())
            self.reading_order_model = ReadingOrderModel(options=ReadingOrderOptions())
            self.heading_hierarchy_model = HeadingHierarchyModel(
                options=opts.heading_hierarchy_options
            )
            self.enrichment_pipe = []

    options = PdfPipelineOptions(
        artifacts_path=config.artifacts_path,
        do_ocr=config.ocr,
        do_table_structure=False,
        enable_remote_services=False,
        allow_external_plugins=False,
        layout_options=LayoutObjectDetectionOptions(
            engine_options=OnnxRuntimeObjectDetectionEngineOptions()
        ),
        accelerator_options=AcceleratorOptions(
            device=AcceleratorDevice.CPU, num_threads=config.threads
        ),
    )
    if config.ocr:
        options.ocr_options = TesseractCliOcrOptions(
            lang=list(config.languages),
            tesseract_cmd=str(config.tesseract_cmd),
            path=str(config.tessdata_path),
        )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(
                backend=DoclingParseDocumentBackend,
                pipeline_cls=LocalPdfPipeline,
                pipeline_options=options,
            ),
        },
    )
