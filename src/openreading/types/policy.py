"""The `policy:` block as a typed object, mirroring `strategy-config.v0.4.json`.

One key: `backends`, a flat list of backend ids in preference order. It is both halves of what a
policy used to say, which backends may run and which runs first, and it is a statement core can
honour exactly because the caller made it.

The nine keys before it asked core to enforce a compliance posture from a per-vendor table this
package keeps in its own source: whether each vendor signs a BAA, trains on customer data, or
retains a document for so many hours. Nothing here can observe any of that, so a stale entry did
not fail loudly, it routed a document to a backend the operator believed was excluded and the run
succeeded. Core holds no fact it cannot verify, and a constraint core cannot check is one it must
not appear to enforce.

An operator who cares about compliance already knows their own posture and which vendors they
hold agreements with. `backends: [aws-textract, pymupdf]` is that conclusion, written by the one
party who can reach it.

Order matters and an empty list permits nothing, which is the fail-closed direction compliance
had. An absent list is not an empty one: absent means no restriction from this source.

Both a schema and a model, because the two doors differ. A file is bytes, so only a schema can
speak about it. A Python object skips the schema, and skipping it used to buy permission rather
than an error, so `strict=True` refuses a value of the wrong type and `extra="forbid"` refuses the
misspelled key that used to be dropped in silence.

`tests/test_policy_validation.py` pins this model's fields equal to the schema's.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Policy(BaseModel):
    """The one key, typed.

    Three settings, each closing a different door into the same object. `extra="forbid"` so a
    misspelling is an error rather than a silently dropped constraint, which is what matters most
    here: `backend: [pymupdf]` for `backends:` would otherwise mean "no restriction" instead of
    "only pymupdf". `strict=True` so the CONSTRUCTOR does not coerce, because `Policy(...)` is the
    shortest way anyone embedding this package will build one and it must not accept a shape the
    file path rejects. `validate_assignment=True` so a field set after construction is checked
    too: the model outlives the call that made it, and an unvalidated assignment is a second door
    into an object the first door already checked.
    """

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    backends: list[str] | None = None

    def get(self, key: str):
        """Read one key by name, as the flat-dict callers did before this model existed."""
        return getattr(self, key, None)


def coerce_policy(policy: Policy | dict | None) -> Policy | None:
    """Return a validated `Policy`, or None for "no policy".

    `strict=True` rather than pydantic's default coercion: the HTTP surface type-checks the same
    values against the request schema before pydantic sees them, and JSON Schema does not coerce.
    Without it a value would be accepted here and rejected over the wire, which is one policy
    meaning two things depending on which door it came through.
    """
    if policy is None:
        return None
    # Re-validated, never trusted for being the right class. `model_construct` builds a `Policy`
    # with no validation at all, by design, so an instance says who built it and nothing about
    # what is in it. Round-tripping through `model_dump` costs one dict per call and removes the
    # question.
    # `warnings=False`: dumping a `model_construct`ed object with a wrong-typed field makes
    # pydantic warn about the serialization. That is the case this round-trip exists to catch, and
    # the validation below rejects it a line later, so the warning is noise about a value that is
    # already on its way to an error.
    raw = policy.model_dump(warnings=False) if isinstance(policy, Policy) else policy
    return Policy.model_validate(raw, strict=True)
