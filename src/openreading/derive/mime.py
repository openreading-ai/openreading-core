"""The one place a document's media type is decided.

Core used to carry six extension-to-MIME tables, five of which turned an unrecognised input into
`application/pdf`. `api._MIME_BY_EXT` listed nine extensions and defaulted the rest; the Claude,
Gemini and Mistral adapters each guessed again on their own. Seventeen of the twenty-six
extensions `openreading.batch.sources` already knew about resolved to PDF, so an `.svg`, an
`.xml` and an `.epub` all reached a backend labelled as PDFs.

That is a worse failure than misjudging a capability. A wrong capability guess produces a vendor
error the fallback chain already handles. A wrong LABEL produces a vendor that accepts the bytes
and returns confident output: nothing raises, no fallback fires, and the caller gets a wrong
answer with no signal that anything happened. Core may guess about what a backend can do. It may
not lie about what the caller handed it.

Precedence, and the reasoning for the order
-------------------------------------------
1. The caller's explicit `mime_type`. The one override. They know things core cannot.
2. `puremagic` over the leading bytes.
3. `mimetypes` over the filename.
4. `None`.

Content beats filename because a filename is a claim and the bytes are a fact. Putting the
extension first means detection never runs on the inputs that need it most, which are the ones
whose name is wrong: a real PDF saved as `scan.txt` resolves to `application/pdf` here and would
have resolved to `text/plain` the other way round.

Step 4 is the substance. An unknown input stays unknown rather than becoming a PDF, so a backend
can refuse it. `openreading.derive.mime` never invents a type, and neither may an adapter: one
that must name a media type on the wire sends what its own API requires and lets the vendor
refuse.

Why `puremagic`
---------------
MIT, pure Python, no system package. `python-magic` wraps libmagic and is more widely used, but a
native dependency lands on every install and every CI image for a gain this does not need.
`filetype` is pure Python too and more downloaded, but it has not shipped a commit since May 2025
with 65 issues open. Pinned `>=1.30,<2` because puremagic 2.x requires Python 3.12 and this
package supports 3.11; when that floor moves, 2.x is an ordinary bump.

Environment variables this module reads
---------------------------------------
None.
"""

from __future__ import annotations

import mimetypes

# The bytes a signature check needs. Every format puremagic recognises declares itself well inside
# this, and reading less keeps the check cheap for a caller who passed a whole document.
_SNIFF_BYTES = 4096


def resolve_mime_type(
    *,
    mime_type: str | None = None,
    filename: str | None = None,
    data: bytes | None = None,
) -> str | None:
    """The document's media type, or `None` when core cannot honestly say.

    `None` is a real answer and callers must handle it. It means "core does not know", which is
    the truth for an unrecognised input, and it is what lets a backend refuse rather than be told
    the bytes are something they are not.
    """
    if mime_type:
        return mime_type

    if data:
        sniffed = _sniff(data[:_SNIFF_BYTES])
        if sniffed:
            return sniffed

    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed

    return None


def _sniff(head: bytes) -> str | None:
    """The media type of these bytes by signature, or None.

    Every failure mode is "core does not know": puremagic raises for bytes it cannot place, and a
    match may carry no MIME type at all (it knows the extension but not the media type). Both
    collapse to None so the filename gets its turn.
    """
    try:
        import puremagic
    except ImportError:  # pragma: no cover - puremagic is a base dependency
        return None

    try:
        matches = puremagic.magic_string(head)
    except Exception:  # noqa: BLE001 - puremagic raises PureError for "no match", among others
        return None

    for match in matches or ():
        found = getattr(match, "mime_type", None)
        if found:
            return found
    return None
