# Resume identity: what belongs in `config_hash`

**Status:** DESIGN, revision 1. One item is proposed and not built. One is declined with reasons.
**Reviewed against:** `feat/policy-one-yaml`, 2026-09-06, after the six enforcement fixes landed.

This record is the surviving half of `design/policy-enforcement-follow-up.md`. That record's six
findings are built and its file is deleted, because a design record does not outlive the feature it
proposed. Two of its statements about resume identity are not built, and one of those should not
be, so they live here rather than disappearing with it.

## What a resume identity is for

`config_hash` answers one question: is the run about to continue the same run that armed? A
mismatch refuses the resume (`ledger.header.HeaderMismatch`). Too little in the digest and a run
resumes under a configuration it never started under. Too much and an unrelated change refuses a
resume that was perfectly safe, which teaches operators to work around the check.

It currently folds in the pruned tree, the effective compliance, the `RouterConfig`, a descriptor
digest per backend the router classified, and the `policy:` block as written.

## Proposed: the effective `optimize_for`

`optimize_for` changes stage-3 ordering, so it changes which backend runs first and therefore what
a resumed walk does next. It is not in the digest. A file that changes `optimize_for: cost` to
`accuracy` between arm and resume produces a different chain order and the same identity.

The written `policy:` block IS in the digest, so a change made in the FILE is already caught. The
gap is narrower than it looks: it is a request-supplied `optimize_for` differing between the
original call and the resumed one. A resume takes no request, so this can only happen through the
ledger's own stored request, which does not change. The remaining case is a preference the file
supplied for one run and the caller supplied for the other.

Build it by folding the effective `routing.optimize_for` into the payload beside the effective
compliance. It is one line and one test. It is unbuilt because no reachable path produces the
divergence today, and a digest input that cannot change is a claim the tests cannot make honest.

## Declined: ordered eligible backend ids

The follow-up record asked for the ordered eligible ids in the hard identity. They should not go
in.

The ids are an OUTPUT of routing over the live registry, not an input the operator controls. They
reorder when a backend's `integration_priority` changes, when an adapter is added or removed from
the installed extras, and when a descriptor is edited. None of those is a policy change, and each
would refuse every in-flight resume on the machine.

The failure the ordered ids are meant to catch is already covered by the inputs that produce them:
the descriptor digests catch a changed descriptor, the effective compliance catches a changed
constraint, and the written policy block catches a changed file. A digest over the result as well
as over every input is not a stronger check. It is the same check plus the environment's own
churn.

If a case appears where the order changes with no input change, that is a bug in stage 3 rather
than a reason to widen the identity.

## Not a gap: provenance in the ledger

The follow-up record proposed storing the caller's request separately from the effective request,
so a resume could tell which source supplied a value. That is a header schema change and a journal
version bump.

It is not needed for the failure it named. Removing a file constraint between arm and resume is
caught, because the block as written is in the digest and its absence changes it. Storing both
requests would let a resume REPORT which source a value came from, which is a debugging
affordance and not an enforcement one. Build it when something asks to display it.
