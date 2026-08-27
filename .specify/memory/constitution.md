<!--
Sync Impact Report
Version change: none (unfilled template) → 1.0.0
Bump rationale: MAJOR — initial ratification. The prior file was the unmodified
scaffold with every [PLACEHOLDER] intact, so this establishes governance rather
than amending it.
Modified principles:
  [PRINCIPLE_1_NAME] → I. Smallest Change That Works (NON-NEGOTIABLE)
  [PRINCIPLE_2_NAME] → II. One Runnable Check Per Behaviour Change
  [PRINCIPLE_3_NAME] → III. Measured, Not Guessed
  [PRINCIPLE_4_NAME] → IV. Upstream Contract Fidelity
  [PRINCIPLE_5_NAME] → V. Consumer-Visible Change Is Documented
Added sections:
  Additional Constraints (was [SECTION_2_NAME])
  Development Workflow & Quality Gates (was [SECTION_3_NAME])
Removed sections: none
Deferred TODOs: none
-->

# pipecat-slng Constitution

## Core Principles

### I. Smallest Change That Works (NON-NEGOTIABLE)

Every change MUST be the shortest diff that makes the behaviour correct. Before writing
code, walk the ladder and stop at the first rung that holds: does this need to exist at
all; does the standard library cover it; does the Pipecat base class already do it; does
an already-installed dependency solve it; can it be one line. Taking a higher rung than
necessary is a defect, not thoroughness.

Prohibited without an explicit request: an abstraction with one implementation, a factory
for one product, a configuration layer for a value that never changes, scaffolding for
speculative future need. A deliberate simplification with a known ceiling MUST carry a
`ponytail:` comment naming the ceiling and the upgrade path.

Never simplify away input validation at a trust boundary, error handling that prevents
data loss, or an explicitly requested behaviour.

Rationale: this package is a thin adapter between two contracts we do not own. Surface
area added here is surface area a downstream consumer must reason about, and every knob
is a support burden. When a base class already implements the machinery, reimplementing
it locally is how the plugin drifts out of sync with Pipecat.

### II. One Runnable Check Per Behaviour Change

Any change to observable behaviour MUST leave behind exactly one runnable check that
fails if the behaviour regresses. Checks go in the existing test files — `tests/test_stt.py`,
`tests/test_tts.py`, and `tests/test_live_smoke.py` for behaviour only provable against the
live bridge. No new test framework, no fixtures beyond the `patch_ws` fake WebSocket in
`tests/conftest.py`, no per-function suites.

The check MUST assert the observable outcome a consumer depends on, not the internal call
that produces it. Trivial one-liners need no check.

Rationale: the failure mode this package has already shipped is a test that asserts on a
field the upstream protocol does not send — green forever, proving nothing. A check that
pins the consumer-visible outcome cannot pass for the wrong reason.

### III. Measured, Not Guessed

A latency constant, a timing default, or a performance knob MUST NOT be invented. Each
one is either measured or left out with its reason recorded.

- Measurements MUST use several samples per configuration. One run proves nothing;
  two runs of an identical build have differed by more than most single settings are worth.
- Measurements MUST compare the specific span the change touches, not end-to-end, which
  yields too few samples per call and swamps the signal.
- A knob that cannot be shown to help MUST be backed out and documented with the reason
  it was rejected. A rejected knob with its evidence is a deliverable of equal standing to
  an accepted one, because it spares the next person a round of testing.
- When measurement is blocked, the dependent work MUST ship without it and be recorded as
  unblocked-but-unmeasured rather than filled with a plausible number.

Rationale: a wrong constant is worse than a missing one. A missing constant produces a
documented fallback and a warning; a fabricated constant produces silent, confident,
unfalsifiable misbehaviour.

### IV. Upstream Contract Fidelity

This package speaks two contracts it does not control: the SLNG bridge WebSocket protocols
and the Pipecat service base classes. Neither may be assumed.

- A protocol field MUST NOT be read or written unless it appears in the SLNG bridge
  reference for that endpoint. Fields that exist only because one upstream provider's raw
  payload passes through are per-route accidents and MUST NOT be relied upon for control
  flow.
- Base-class semantics MUST be verified in the installed Pipecat source at the version
  under test before code depends on them. Cite file and line in the spec.
- A feature that engages conditionally MUST report whether it engaged. Unobservable
  behaviour is unfalsifiable behaviour and MUST NOT ship.

Rationale: both contracts move independently of this repo, and a plausible-looking field
name is the cheapest way to write a branch that is dead on every route that matters.

### V. Consumer-Visible Change Is Documented

`CHANGELOG.md` MUST be updated in the same change as any behaviour a consumer can observe,
in Keep-a-Changelog form under semantic versioning. A change to conversational timing,
turn boundaries, or connection lifecycle affects every existing downstream user and MUST be
stated unmissably — named in its own entry, not folded into a list.

Each entry MUST record the observed behaviour, not only the code that changed, and MUST name
the Pipecat version it was verified against.

Rationale: downstream bots inherit this package's timing. A turn that ends sooner is a
behaviour change even when it is strictly an improvement, and a consumer who is surprised
by it cannot tell an improvement from a regression.

## Additional Constraints

**Dependencies.** No new runtime dependency. The declared floor `pipecat-ai>=1.3.0` holds
unless an API the work requires is genuinely absent below it; raising the floor requires
naming the absent API. `websockets` and `aiohttp` are the only other runtime deps.

**Target version.** Correctness is judged against the Pipecat version the Unmute CLI's
`pipecat` target pins — currently 1.7.0 — while the declared floor stays supported. Where
the two differ in the semantics a change relies on, the spec MUST say so.

**API surface.** New constructor keyword arguments require a measured justification under
Principle III. Where an equivalent knob already exists in the sibling LiveKit-side SLNG
plugin, the name MUST match it, so a parameter already emitted by an upstream generator
becomes live rather than silently swallowed into `**kwargs`. Anything holding a resource
open per session defaults to off.

**Boundaries.** The Unmute CLI is out of scope for changes made here; work it needs is
recorded for its owner, not performed. Connection handling is not refactored beyond what a
specified fix requires.

## Development Workflow & Quality Gates

**Gates.** `ruff check`, `ruff format --check`, `ty`, and `pytest` MUST all pass before a
change is considered done. Offline tests MUST pass with no network and no credentials. Live
tests MUST stay gated behind environment variables and MUST skip cleanly when unset.

**Spec before code.** Work begins from a spec that carries, for every item: the observed
behaviour, the file and line, the evidence, and the check that fails if the fix regresses.
An item whose evidence does not survive contact with the source is corrected in the spec
before implementation, not worked around in code.

**Staged delivery.** Changes ship in stages ordered by payoff, smallest and highest-value
first, reviewed and merged before larger work starts. A large item MUST NOT be bundled with
a small high-payoff fix that could stand alone.

**Overlapping items.** When two candidate fixes may address the same symptom, the simpler
one ships first and is measured. The larger one is built only if the measurement shows the
simpler one did not cover it, and the spec records that ordering as a decision rather than
building both on speculation.

## Governance

This constitution supersedes other development practices for this repository. Where a
principle and a convenience conflict, the principle wins.

**Amendments.** An amendment MUST be recorded in this file with a Sync Impact Report, a
version bump, and the rationale for the bump. Versioning follows semantic versioning:
MAJOR for a backward-incompatible removal or redefinition of a principle or governance
rule, MINOR for a new or materially expanded principle or section, PATCH for clarification
and wording that changes no obligation.

**Compliance.** Every change is reviewed against these principles. A reviewer MUST be able
to point at the runnable check required by Principle II and the evidence required by
Principle III. Added complexity MUST be justified in the review, and unjustified complexity
is grounds for rejection on its own.

**Precedence.** Principle I governs how much is built. It never overrides Principle II's
check, Principle III's evidence, or the validation and error handling Principle I itself
protects — a change is not smaller for having dropped its proof.

**Version**: 1.0.0 | **Ratified**: 2026-08-26 | **Last Amended**: 2026-08-26
