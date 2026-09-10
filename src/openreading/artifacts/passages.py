"""Project normalized pages into contiguous, exact source spans without invented geometry."""

from collections.abc import Iterator

from openreading.artifacts.constants import MAX_PASSAGE_CHARS
from openreading.artifacts.models import Passage, TextOrigin
from openreading.types.response import NormalizedResponse


def iter_passages(
    response: NormalizedResponse, origins: dict[str, TextOrigin] | None = None
) -> Iterator[Passage]:
    for page in sorted(response.document.pages or [], key=lambda p: p.page_number):
        blocks = sorted(
            enumerate(page.blocks or []),
            key=lambda pair: (
                pair[1].reading_order if pair[1].reading_order is not None else pair[0],
                pair[0],
            ),
        )
        sources = [(b.text, b.bbox, b.id, "block_text") for _, b in blocks if b.text]
        if not sources and page.text:
            sources = [(page.text, None, None, "page_text")]
        for index, (text, bbox, block_id, kind) in enumerate(sources):
            assert text is not None
            start = segment = 0
            while start < len(text):
                end = min(start + MAX_PASSAGE_CHARS, len(text))
                if end < len(text):
                    whitespace = [i for i in range(start, end) if text[i].isspace()]
                    if whitespace:
                        end = whitespace[-1] + 1
                yield Passage(
                    evidence_id=f"p{page.page_number:04d}-b{index:04d}-s{segment:04d}",
                    page=page.page_number,
                    text_origin=(origins or {}).get(str(page.page_number)),
                    block_index=index,
                    segment_index=segment,
                    source_kind="block_text" if kind == "block_text" else "page_text",
                    text_start=start,
                    text_end=end,
                    text=text[start:end],
                    bbox=bbox,
                    source_block_id=block_id,
                )
                start = end
                segment += 1
