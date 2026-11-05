# mriDICOMpipeline — EGM Production-Hardening Intro Brief

**Date:** November 4, 2026. **Current version:** 0.1.0, Alpha. **State schema:** 2.  
**Status:** input brief for design/review; proposed changes are not yet approved.

## Objective

Turn the existing study-agnostic MRI DICOM intake/conversion package into a rigorously validated, production-supported research workflow. Begin by challenging its current behavior and documenting precise guarantees, failure modes, and evidence gaps. Preserve the incremental batch workflow while resolving safety, scientific integrity, recovery, and support decisions.

The detailed baseline is `mriDICOMpipeline-current-state-spec.md`. The file-hash manifest is `current-source-manifest.json`. Read those with the public source checkout; prior chat history is unnecessary.

## What exists now

The package exposes `mri-dicom-pipeline prepare` and `convert`, plus equivalent Python-module and source-script entry points. Preparation discovers session ZIPs, validates subject/date filename identity against configurable CSV columns, organizes archives, extracts eligible DICOM objects, and checkpoints inventories. Conversion uses prepared state and external dcm2niix, filters/organizes/compresses outputs, preserves available diffusion companions, and records complete or partial results.

One coordinator writes persistent state, status CSVs, logs, and run history. Thread-based subject workers process dates serially within each subject, with external converter processes providing conversion parallelism. The intended deployment is one node, optionally allocated through Slurm. Ordinary cached reruns avoid image-content work. Explicit `_2` and higher session suffixes are preserved throughout the pipeline.

The previous development verification recorded 30 passing synthetic test cases on Windows/Python 3.12, successful local package builds/content checks, and installed-command smoke checks. A metadata-only 1,000-session test prevented content hashing/header reads/converter calls on ordinary reruns. CI is configured for Linux/Windows and Python 3.10/3.12; remote execution and broad real-converter/scanner compatibility are not established.

## User priorities

1. Reliable repeated processing of new batches without expensive rescanning of completed images.
2. Deterministic study-independent identity and explicit same-day session numbering.
3. Preservation of original inputs and last-good output, with clear handling of conflicts and failures.
4. Concurrency on one node without shared-file corruption or exceeding agreed resources.
5. Stable report locations and clear, resumable operational state.
6. Scientific correctness of converted images and honest privacy guarantees.
7. A maintainable, testable, documented, appropriately licensed GitHub/Python package.

## Important current limitations to challenge

The pipeline is not BIDS generation, clinical software, a PACS client, an anonymization/defacing service, or a distributed processing platform. Metadata filtering does not certify privacy. Structural NIfTI checks do not prove geometry/scaling correctness. Duplicate detection is session-scoped and byte-based, not semantic or overlap-based.

Fast caching trusts receipt/source metadata and directory mtimes; audit has a different cost. Converter/tool/configuration changes are not fully represented in successful cache identity. Publication, source relocation, and checkpointing span multiple filesystem operations and are not a proven all-crash transaction. Advisory locking is keyed by project root and needs validation on actual shared storage. SIGTERM, process descendants, corrupted state, relocation, quotas, and abandoned publication directories need deliberate policies.

The detailed specification records a discrepancy: combining dry-run and audit can hash cached files despite the broad no-hashes dry-run description. It also documents that changed canonical archive contents can follow a replacement path distinct from an incoming-copy conflict. Assess these explicitly rather than assuming universal no-read/no-replacement guarantees.

## Requested design/review work

First verify the descriptive baseline against the source. Then propose a production-hardening design that resolves requirements before implementation. Review through these lenses: data loss and crash recovery; concurrent/shared filesystem safety; scientific image/companion correctness; metadata/privacy and untrusted-input boundaries; cache/provenance/migrations; scaling/resource budgets; operator reporting; compatibility, packaging, and release maintenance.

Use the baseline's R01–R20 review register and T01–T18 stress-test matrix as starting coverage, not as a limit. Prioritize findings that can cause false success, image corruption, identity reassignment, input loss, unrecoverable state, or sensitive-data disclosure. Distinguish reproduced defects from static hypotheses and unresolved product decisions.

For every finding provide source location/function, triggering scenario, impact/severity, evidence/confidence, design alternatives, recommendation, and a concrete regression or acceptance test. For every proposed guarantee identify the test and deployment evidence that would establish it. Set measurable performance/resource targets instead of promising that all environments will match a local benchmark.

## Decisions requiring owner review

Supported OS/Python/converter/scanner/storage matrix; immutable versus replaceable canonical inputs; upload readiness protocol; partial-overlap policy; last-good versus latest-attempt state model; scientific validation scope; permitted NIfTI sanitization changes; diffusion/classification policy; migration and repair strategy; resource limits and retention; release license and support commitments.

Use synthetic or explicitly approved de-identified fixtures. Keep private participant rosters, archives, DICOMs, and historical reports out of review artifacts and release packages. Preserve existing data/state during analysis. Produce an implementation plan only after the design trade-offs are made explicit and approved.

## Expected outputs from the next phase

- A source-grounded discrepancy/risk report with reproducible tests where possible.
- An approved production behavior contract and architecture, including migration implications.
- A prioritized implementation plan tied to invariants and acceptance tests.
- A validation matrix covering fault injection, real converters, scientific correctness, supported platforms/storage, realistic scale, privacy channels, and clean release artifacts.
- An operational runbook and objective release gates, with unsupported claims removed.

EGM context: [project README](https://github.com/google/elephant-goldfish-model/blob/main/README.md) and [workflow guide](https://github.com/google/elephant-goldfish-model/blob/main/docs/getting_started.md). This brief prepares that workflow; no EGM review or production certification is implied.
