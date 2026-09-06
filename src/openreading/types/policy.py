"""The `policy:` block as a typed object, mirroring `strategy-config.v0.3.json`.

A policy is the short list of things a backend must declare before it may read a document. The
schema is the contract for a policy that arrives as file text, and it refuses an unknown key or a
value of the wrong type where the file is read. This model is the same contract for a policy that
never passes through a file: a dict handed straight to `openreading.config.apply`, a
`StrategyConfig` a caller builds with `model_validate`, or a `Policy` constructed here.

Both halves are needed because the two doors are genuinely different. A file is bytes, so only a
schema can speak about it. A Python object skips the schema entirely, and skipping it used to buy
permission rather than an error: `bool("false")` is `True`, so a quoted boolean under
`allow_unverified_compliance` switched the fail-closed posture ON, and a bare
`train_optout_confirmed: "aws-textract"` became a frozenset of that string's characters,
confirming no backend at all while looking like it confirmed one. `strict=True` on every
validation refuses both, and `extra="forbid"` refuses the misspelled key that used to be dropped
in silence.

`tests/test_policy_validation.py` pins this model's fields equal to the schema's, so a key added
to one and not the other cannot ship. `tests/test_policy_enforcement.py` pins the three doors:
construction, assignment, and an instance handed across a public boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# The five keys that become `request.compliance`, in schema order. Read by
# `openreading.config.union_compliance` and by the parity test, and never re-typed beside either.
COMPLIANCE_FIELDS = (
    "require_baa",
    "no_train_on_data",
    "data_region",
    "require_local",
    "max_retention",
)
# The keys that become `request.routing`. A deliberate subset: `fallback` is chain order, not a
# constraint, so a policy can never reorder someone's chain by naming backends.
ROUTING_FIELDS = ("optimize_for",)
# The keys that become a `RouterConfig`. These three are the only ones that WIDEN the eligible
# set, and each asserts a fact about paperwork rather than a preference about a vendor.
ATTESTATION_FIELDS = ("allow_unverified_compliance", "train_optout_confirmed", "baa_tier_confirmed")


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

    require_baa: bool | None = None
    no_train_on_data: bool | None = None
    data_region: str | None = None
    require_local: bool | None = None
    max_retention: str | None = None
    optimize_for: str | None = None
    allow_unverified_compliance: bool | None = None
    train_optout_confirmed: list[str] | None = None
    baa_tier_confirmed: list[str] | None = None

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
