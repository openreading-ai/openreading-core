"""One resolver, and it never guesses PDF.

`design/format-agnostic-intake.md` part 2. Core had six extension-to-MIME tables and five of them
defaulted an unknown input to `application/pdf`, so an `.svg`, an `.xml` and an `.epub` all
reached a backend labelled as PDFs. A wrong label is worse than a wrong capability guess: the
vendor accepts the bytes and returns confident output, nothing raises, no fallback fires, and the
caller gets a wrong answer with no signal.

Two rules are tested here.

**Content beats filename.** A filename is a claim and the bytes are the fact, so sniffing runs
before `mimetypes`. Ordering it the other way means detection never runs on the inputs that need
it most, which are exactly the ones whose name lies.

**Unknown stays unknown.** The answer is `None`, never `application/pdf`. An adapter that must
name a media type on the wire sends what its own API needs and lets the vendor refuse; it never
substitutes PDF for an input core could not identify.
"""

from __future__ import annotations

import mimetypes

import pytest

from openreading.derive.mime import resolve_mime_type


@pytest.mark.parametrize(
    "name,expected",
    [
        ("a.pdf", "application/pdf"),
        ("a.png", "image/png"),
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpeg"),
        ("a.tiff", "image/tiff"),
        ("a.svg", "image/svg+xml"),
        ("a.xml", "application/xml"),
        ("a.html", "text/html"),
        ("a.txt", "text/plain"),
        ("a.epub", "application/epub+zip"),
        ("a.webp", "image/webp"),
        ("a.gif", "image/gif"),
        ("a.bmp", "image/bmp"),
        (
            "a.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("a.odt", "application/vnd.oasis.opendocument.text"),
    ],
)
def test_a_filename_resolves_to_its_real_type(name: str, expected: str) -> None:
    """Seventeen of the twenty-six extensions the batch layer already knew resolved to
    `application/pdf` before this change, because the nine-entry table did not list them."""
    assert resolve_mime_type(filename=name) == expected


def test_the_caller_always_wins() -> None:
    """An explicit `mime_type` is the one override. The caller knows something core cannot, and
    saying so must not be second-guessed by a sniffer or an extension."""
    assert (
        resolve_mime_type(mime_type="application/x-private", filename="a.pdf", data=b"%PDF-1.4\n")
        == "application/x-private"
    )


def test_content_beats_a_lying_filename() -> None:
    """The case a resolver exists for. A real PDF named `.txt` is a PDF, and answering
    `text/plain` here would hand a document to a backend under the wrong label."""
    assert resolve_mime_type(filename="scan.txt", data=b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n") == (
        "application/pdf"
    )


def test_an_unrecognisable_input_is_unknown_not_pdf() -> None:
    """The substance of the change. Bytes with no signature and no filename are `None`, so a
    backend can refuse them rather than being told they are a PDF."""
    assert resolve_mime_type(data=b"\x00\x01\x02not a known format at all") is None
    assert resolve_mime_type() is None


def test_an_unknown_extension_is_unknown() -> None:
    assert resolve_mime_type(filename="ledger.qqq") is None


def test_the_stdlib_still_owns_the_extension_table() -> None:
    """`mimetypes` is the filename half, so a format Python learns about is one core learns about
    too. This is the whole reason the hand-maintained table was deletable."""
    for ext in (".svg", ".xml", ".epub", ".heic", ".avif"):
        assert resolve_mime_type(filename=f"x{ext}") == mimetypes.guess_type(f"x{ext}")[0]


def test_no_module_keeps_its_own_extension_table() -> None:
    """The six tables collapse into this one. A second table reappearing anywhere is the defect
    this change exists to remove, so name the sites rather than trusting a reviewer to notice."""
    import inspect

    from openreading import api
    from openreading.adapters.anthropic_claude import adapter as claude
    from openreading.adapters.mistral_ocr import adapter as mistral

    assert not hasattr(api, "_MIME_BY_EXT")
    assert not hasattr(claude, "_MEDIA_TYPES")
    assert not hasattr(claude, "_FORMAT_ALIASES")
    assert not hasattr(mistral.MistralOCRAdapter, "_guess_mime_type")
    # And no adapter reintroduces the PDF default it just lost.
    for module in (claude, mistral):
        assert 'or "application/pdf"' not in inspect.getsource(module)
