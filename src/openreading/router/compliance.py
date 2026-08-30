"""Stage-1 compliance hard-filter (internal/research/openreading/routing_and_compliance.md §4.4).

This is a pass/fail gate applied FIRST and never traded off against quality/cost. Any field
relevant to an active constraint that is UNVERIFIED (None/absent) fails closed unless the
deployment sets `allow_unverified_compliance=True`. Explicit negatives (trains=yes, no BAA)
always drop regardless of that flag. A CONDITIONAL positive — a BAA or a no-train guarantee the
vendor offers only on a higher plan — counts only where the operator confirmed the precondition
holds here (`baa_tier_confirmed` / `train_optout_confirmed`), and that reliance is surfaced on the
response rather than left silent.

Request-side constraints come from `request.compliance` (the vendored wire schema): require_baa,
no_train_on_data, data_region, require_local, max_retention. Backend-side facts come from the
adapter's descriptor.compliance, whose `extra="allow"` lets an adapter carry the richer §4.3
fields (max_retention_hours, train_opt_out_precondition, zdr_flag, phi_path_constraints).

`runs_fully_local=True` is a static architectural claim (no code path in this adapter makes a
third-party network call), not a promise about a specific deployment's configuration. A backend
whose descriptor declares a `config_spec` field named `endpoint` (docling, qwen-vl: containers
the OPERATOR points somewhere) is only trusted as local if that endpoint actually resolves to
loopback right now — read the same way the credential broker reads it, override env first, the
descriptor's own `env` list second. An adapter with no such field (pymupdf, tesseract: in-process
libraries with nothing to point anywhere) is trusted on the static claim alone. This closes a real
gap: pointing `DOCLING_SERVE_URL` at a remote host previously still passed `require_local`, and
every OTHER stage-1 check this module skips for a "local" backend (BAA, training, region,
retention) skipped right along with it.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from openreading.credentials import _slug_env
from openreading.types.descriptor import AdapterDescriptor
from openreading.types.request import Compliance

_BAA_OK = frozenset({"yes"})
# hipaa_baa='tier_gated' means the vendor offers a BAA on a higher plan — NOT a BAA in force for
# this deployment. It satisfies require_baa only for the ids the operator listed in
# RouterConfig.baa_tier_confirmed, exactly as trains='opt_out' needs train_optout_confirmed.
_BAA_TIER_GATED = "tier_gated"
_NO_TRAIN_OK = frozenset({"no", "na_local"})

# response.warnings[] code for a require_baa that rests on the operator's tier-gate confirmation.
BAA_TIER_CONFIRMED_WARNING = "baa_tier_confirmed"


@dataclass(frozen=True)
class DropReason:
    stage: int  # 1 = compliance, 2 = capability
    code: str
    detail: str


@dataclass
class RouterConfig:
    # Default False = UNVERIFIED compliance fields fail closed (the safe posture).
    allow_unverified_compliance: bool = False
    # Backend ids whose training opt-out the operator has CONFIRMED applied (e.g. the AWS
    # Organizations AI-services opt-out policy for Textract). Empty => opt_out fails closed.
    train_optout_confirmed: frozenset[str] = field(default_factory=frozenset)
    # Backend ids whose TIER-GATED BAA the operator has CONFIRMED is signed and in force for this
    # deployment's plan (e.g. Reducto Growth+). Empty => hipaa_baa='tier_gated' fails closed.
    baa_tier_confirmed: frozenset[str] = field(default_factory=frozenset)


def parse_retention_hours(value: str | None) -> float | None:
    """'zero'/'0' -> 0.0, '48h' -> 48.0, '24' -> 24.0, 'zdr' -> 0.0. None/other -> None."""
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("zero", "zdr", "none", "0", "0h"):
        return 0.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*h?", v)
    return float(m.group(1)) if m else None


def _extra(desc: AdapterDescriptor, key: str, default=None):
    # explicit field first (typed), then any forward-compat extra
    val = getattr(desc.compliance, key, None)
    if val is not None:
        return val
    return (desc.compliance.model_extra or {}).get(key, default)


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback_host(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a real hostname, not a loopback literal — trust neither


def _endpoint_config_field(desc: AdapterDescriptor):
    return next((f for f in desc.config_spec if f.key == "endpoint"), None)


def _resolves_to_loopback(desc: AdapterDescriptor) -> bool:
    """Whether a backend claiming runs_fully_local, and whose descriptor names an operator-set
    `endpoint` config field, is actually configured to reach one right now. True when: the backend
    has no such field (an in-process library has nothing to point anywhere); the field is unset
    (not yet configured, so nothing has proven it points off-box); or the configured value's host
    is loopback. False only when a real, non-loopback host is configured — the one state where the
    static `runs_fully_local=True` claim and this deployment's actual wiring disagree."""
    field = _endpoint_config_field(desc)
    if field is None:
        return True
    value = os.environ.get(_slug_env(desc.id, field.key))
    if not value:
        for name in field.env:
            value = os.environ.get(name)
            if value:
                break
    if not value:
        return True
    host = urlsplit(value).hostname
    return host is not None and _is_loopback_host(host)


def evaluate(
    req: Compliance | None, desc: AdapterDescriptor, config: RouterConfig | None = None
) -> DropReason | None:
    """Return None if the backend survives the compliance filter, else the DropReason."""
    cfg = config or RouterConfig()
    if req is None:
        return None
    c = desc.compliance
    local = bool(c.runs_fully_local) and _resolves_to_loopback(desc)
    allow_unverified = cfg.allow_unverified_compliance

    # offline / local-only: data may never leave the caller's environment.
    if req.require_local and not local:
        return DropReason(1, "not_local", "require_local set but backend is not fully local")

    # BAA / PHI path: local is the BAA-free PHI path; else needs a BAA actually in force —
    # hipaa_baa='yes', or 'tier_gated' plus this deployment's confirmation for that backend.
    if req.require_baa and not local and not _baa_in_force(desc, cfg):
        return DropReason(1, "no_baa", _no_baa_detail(desc))

    # no-train: no|na_local pass; opt_out passes only if the operator confirmed the opt-out;
    # 'unverified' (vendor claim unconfirmed) passes only under allow_unverified_compliance.
    if req.no_train_on_data and not local:
        trains = c.trains_on_customer_data
        if (
            trains in _NO_TRAIN_OK
            or (trains == "opt_out" and desc.id in cfg.train_optout_confirmed)
            or (trains == "unverified" and allow_unverified)
        ):
            pass
        elif trains == "unverified":
            return DropReason(
                1,
                "trains_unverified",
                "no_train_on_data set but trains_on_customer_data UNVERIFIED",
            )
        else:
            return DropReason(
                1, "trains_on_data", f"no_train_on_data set but trains_on_customer_data={trains!r}"
            )

    # data residency: local is region-free (egress-free); else region must be covered.
    if req.data_region and not local:
        regions = c.data_region_options or []
        if not regions:
            if not allow_unverified:
                return DropReason(
                    1, "region_unverified", "data_region set but backend regions unverified"
                )
        elif "*" not in regions and not _region_covered(req.data_region, regions):
            return DropReason(1, "region_mismatch", f"{req.data_region!r} not in {regions}")

    # retention ceiling: local can hold nothing; else backend retention must be known and <=.
    if req.max_retention and not local:
        want = parse_retention_hours(req.max_retention)
        have = _extra(desc, "max_retention_hours")
        # A malformed, non-empty ceiling is a caller-input error, not an unverified-backend
        # question: allow_unverified_compliance governs tolerance for the BACKEND's own
        # disclosure, not whether the CALLER's request string parses. Fails closed unconditionally
        # so an unparseable ceiling can never silently pass a backend through unfiltered.
        if want is None:
            return DropReason(
                1, "retention_unparseable", f"max_retention={req.max_retention!r} does not parse"
            )
        if have is None:
            if not allow_unverified:
                return DropReason(
                    1, "retention_unverified", "max_retention set but backend retention unverified"
                )
        elif float(have) > want:
            return DropReason(1, "retention_exceeds", f"backend retains {have}h > required {want}h")

    return None


def _baa_in_force(desc: AdapterDescriptor, cfg: RouterConfig) -> bool:
    baa = desc.compliance.hipaa_baa
    return baa in _BAA_OK or (baa == _BAA_TIER_GATED and desc.id in cfg.baa_tier_confirmed)


def _no_baa_detail(desc: AdapterDescriptor) -> str:
    baa = desc.compliance.hipaa_baa
    if baa == _BAA_TIER_GATED:
        return (
            f"require_baa set but hipaa_baa='tier_gated' and {desc.id!r} is not in "
            "baa_tier_confirmed"
        )
    return f"require_baa set but hipaa_baa={baa!r}"


def baa_tier_confirmation(
    req: Compliance | None, desc: AdapterDescriptor, config: RouterConfig | None = None
) -> str | None:
    """The message for a require_baa that only the operator's tier-gate confirmation satisfies,
    else None. Callers surface it as a `BAA_TIER_CONFIRMED_WARNING` on the response so a PHI run
    never silently rests on a BAA nobody signed."""
    cfg = config or RouterConfig()
    c = desc.compliance
    if req is None or not req.require_baa or (c.runs_fully_local and _resolves_to_loopback(desc)):
        return None
    if c.hipaa_baa != _BAA_TIER_GATED or desc.id not in cfg.baa_tier_confirmed:
        return None
    return (
        f"require_baa satisfied for {desc.id} by operator confirmation alone: hipaa_baa is "
        "'tier_gated' (BAA offered on a higher plan) and this deployment asserts it is in force"
    )


def _region_covered(want: str, regions: list[str]) -> bool:
    """Case-insensitive EXACT match only. Region codes are opaque identifiers, not a hierarchy:
    prefix matching made distinct codes satisfy each other by coincidence (`eastus2` accepted by a
    backend declaring only `eastus`), silently widening a data-residency filter."""
    want = want.lower()
    return any(r.lower() == want for r in regions)
