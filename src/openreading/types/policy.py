"""The `policy:` block as a typed object, mirroring `strategy-config.v0.4.json`.

One key: `backends`, a flat list of backend ids in preference order. It is both halves of what a
policy used to say, which backends may run and which runs first, and it is a statement core can
honour exactly because the caller made it.

The nine keys before it asked core to enforce a compliance posture from a per-vendor table this
package keeps in its own source: whether each vendor signs a BAA, trains on customer data, or
retains a document for so many hours. Nothing here can observe any of that, so a stale entry did
not fail loudly, it routed a document to a backend the operator believed was excluded and the run
succeeded. `design/compliance-removal.md` has the argument; the short version is that core holds
no fact it cannot verify, and a constraint core cannot check is one it must not appear to enforce.

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

# The five keys that become `request.compliance`, in schema order. Read by
# `openreading.config.union_compliance` and by the parity test, and never re-typed beside either.
# The keys that become `request.routing`. A deliberate subset: `fallback` is chain order, not a
# constraint, so a policy can never reorder someone's chain by naming backends.
# The keys that become a `RouterConfig`. These three are the only ones that WIDEN the eligible
# set, and each asserts a fact about paperwork rather than a preference about a vendor.


class Policy(BaseModel):
    """The nine keys, typed.

    Three settings, each closing a different door into the same object. `extra="forbid"` so a
    misspelling is an error rather than a silently dropped constraint. `strict=True` so the
    CONSTRUCTOR does not coerce: pydantic's default accepts `"yes"` for a bool, which is the
    widening the schema refuses on the file path, and `Policy(...)` is the shortest way anyone
    embedding this package will build one. `validate_assignment=True` so a field set after
    construction is checked too: the model outlives the call that made it, and an unvalidated
    assignment is a second door into an object the first door already checked.

    The assignment case is the one that bites hardest. `bool("false")` is `True`, so writing the
    string `"false"` onto `allow_unverified_compliance` turned the fail-closed posture ON, with a
    value whose plain-English intent is off.
    """

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    backends: list[str] | None = None

    def get(self, key: str):
        """Read one key by name, as the flat-dict callers did before this model existed."""
        return getattr(self, key, None)


def coerce_policy(policy: Policy | dict | None) -> Policy | None:
    """Return a validated `Policy`, or None for "no policy".

    `strict=True` rather than pydantic's default coercion: the HTTP surface type-checks the same
    values against `request.v0.2.json` before pydantic sees them, and JSON Schema does not coerce.
    Without it, `require_baa: "yes"` would be accepted here and rejected over the wire, which is
    the same divergence between two spellings of one policy in a new place.
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
