# Bypass Lab: bounded local search and reduction

The page at `/lab/bypasses` now supports generated search and replay-preserving
reduction in addition to manually entered candidates. No additional dependency
is required.

## What the experiment means

Each of the five bundled fixtures declares a policy-side interpretation,
application-side interpretation, forbidden-value predicate and local replay
endpoint. The fixture policy applies that predicate to its own interpretation;
it is not the ShieldLang compiler or an imported `.shl` policy. The replay app
uses a virtual file tree or value echo/storage, not a real vulnerable service.
Both sides share transformation implementations, so replay is integration
evidence rather than an independent proof of transformation semantics.

A finding requires policy acceptance, an application-view predicate violation,
and a matching effect reproduced against the local fixture application. There
is no external destination input. Identical-pipeline fixture E4 is a negative
control. No result establishes a vulnerability in an actual product.

## Search

`POST /api/bypass/search` accepts:

- `fixture_id`: one of the registered local fixtures.
- `seeds`: optional list of 1–8 strings, each at most 2048 UTF-8 bytes.
  Without caller seeds, initial values are derived from the fixture oracle
  (or the fixture app's virtual-file targets), never its preset bypass examples.
- `budget`: candidate/frontier limit, 1–1024; default 128.
- `max_depth`: mutation composition depth, 0–3; default 2.
- `seed`: reproducible mutation-order seed, unsigned 32-bit integer; default 0.
- `max_findings`: 1–5; default 3.
- `minimize`: boolean, default true.
- `reduction_budget`: deletion checks per finding, 1–500; default 80.
- `time_budget_ms`: cooperative deadline, 100–10000; default 5000.

Search uses breadth-first mutation composition, deduplication and a bounded
frontier (at most four times the candidate budget). Its mutation grammar includes
Unicode compatibility delimiters, percent escapes, NUL insertion, HTML entities,
case variants and JSON whitespace. Duplicate-key mutations use declared field
names and oracle values. Caller seeds replace the generated seed corpus.
This is a restricted grammar/mutation search, not exhaustive language solving.

The result reports seeds, configuration, fixture semantics/hash, engine version,
evaluated candidates and local replay count,
mutation provenance, findings and stop reason. Time budgets can truncate otherwise
reproducible runs. In-flight local HTTP replay and server cleanup can exceed the
cooperative deadline; it is not a hard wall-clock termination guarantee.

`space_exhausted` means only the generated space under the declared depth and
mutation operators was exhausted. Budget stops are explicit. No findings never
means the policy or application is safe.

## Reduction

`POST /api/bypass/minimize` takes `fixture_id`, `raw`, optional `budget` (default
100) and `time_budget_ms` (default 5000). Initial input must be replay-confirmed.

The reducer deletes character chunks, then individual characters; encoded
escapes are not treated as indivisible atoms. Raw JSON syntax remains subject
to validation on every trial. A deletion is retained only after local replay
still confirms the finding. Malformed JSON trials fail validation rather than
crashing the experiment.

The result includes original/reduced input and UTF-8 sizes, deletion attempts,
accepted deletions, stop reason and the final replay evidence. A completed run
claims only a single-character deletion fixed point, not a globally shortest witness.
Budget-limited reduction explicitly reports incomplete minimality.

## Page and saved evidence

Choose a fixture and press **Search this experiment**, or type a known finding
and press **Minimize typed candidate**. Search always requires local replay for
findings; the manual replay checkbox applies only to manual evaluation.

Inputs are locked while a run is pending. Editing inputs clears current results.
Search/reduction records use the existing bounded SQLite run store and can be
reopened from Past runs or downloaded as JSON. Older manual records still open.
The experiment is keyed by fixture semantics, not a compiled policy hash.

## Verification

`python -m unittest tests.test_bypass_search tests.test_bypass` covers discovery
with the preset lists removed, independently expected effects, equivalent
pipelines, deterministic ordering, candidate/depth/input bounds, changed-fixture
hashes, reduction, malformed JSON and refusal to report unconfirmed replay.
The lab API tests also cover search/reduction persistence and invalid inputs.

This work does not add arbitrary-policy compilation, external scanning, a
Traffic Lab gateway handoff, or a formal completeness/security proof.

## Additive patch integration

The reviewed additive revision is integrated with the existing public limits,
caller-seed semantics, finding fields, fixture semantics hash and public stop
codes preserved. Search/reduction share a local server and replay deadline.
Failed reduction trials are stored in `trial_error`; the last confirmed input
and evidence remain aligned. HTTP is skipped for search candidates whose model
views already rule out a finding. The added accounting test consequently checks
actual replay attempts rather than requiring HTTP for every evaluated candidate.
