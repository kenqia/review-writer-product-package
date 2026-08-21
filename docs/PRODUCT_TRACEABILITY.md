# Product Traceability

Verification date: `2026-08-21`

Main evidence base / inspected commit: `14ed2f9e1e2770f0fe6d2bdab870c4e3b4c1abbe`

This is a read-only traceability snapshot. It does not copy either source document body,
and it does not turn static paths or unrun tests into implementation or acceptance claims.

## Immutable source references

The source checkout was inspected read-only. The SRS and Design files were untracked at
verification time, so their references are intentionally dirty and unpinned; the source
repository commit is not a file version.

| document | source checkout | source status | source commit SHA | file SHA-256 | verification date | body copied? |
| --- | --- | --- | --- | --- | --- | --- |
| `docs/product/REVIEW_WRITER_SRS.md` | `/home/kenqia/my_folder/review-writer` | `DIRTY_UNPINNED` | `UNPINNED` | `4b24c66c77af15c19a9b83774d5886a43d85d11ddaa01ea98556a8a3979891e2` | `2026-08-21` | No |
| `docs/product/REVIEW_WRITER_DESIGN.md` | `/home/kenqia/my_folder/review-writer` | `DIRTY_UNPINNED` | `UNPINNED` | `2eef3720831460f58b7ab0c591205fbff62ee50a60c1e80448ad79b2b4f77cf9` | `2026-08-21` | No |

## FR traceability

The short descriptions below are taken from the inspected SRS FR headings. Code and test
seams are observable package paths only. Test seams remain `NOT_RUN` unless a fresh run is
explicitly noted below. `PARTIAL` means a bounded seam or focused test node exists but the complete public
contract or acceptance layer is not proven; `NOT_VERIFIED` means no fresh evidence is
available for the stated behavior. No row claims `IMPLEMENTED`.

| FR | code seam | test seam | status | evidence / gap |
| --- | --- | --- | --- | --- |
| `FR-001` — 唯一入口 | `review_writer/agent/public_entry.py::start_or_resume_review`; existing `review_writer/agent/fresh_bootstrap.py::FreshAgentBootstrap.start` | `tests/test_public_agent_entry.py` | `PARTIAL` | Fresh focused evidence is `4/4` for discoverability, fresh/resume mapping and zero-write guards. Optional `rq`/`scope`/`output_format` are accepted and validated but not yet persisted. Engineering evidence only; Product Use, `PUBLIC_E2E` and `HUMAN_ACCEPTANCE` remain `HOLD`. |
| `FR-002` — fresh/resume | `review_writer/agent/public_entry.py::start_or_resume_review`; `review_writer/product_foundation/service.py::VersionContext`; `review_writer/product_foundation/project_root.py` | `tests/test_public_agent_entry.py` | `PARTIAL` | Fresh focused evidence covers public fresh mapping and zero-write resume/source mismatch. Cold resume/current/write-set proof beyond this bounded seam and full public-flow evidence remain missing; Product Use, `PUBLIC_E2E` and `HUMAN_ACCEPTANCE` remain `HOLD`. |
| `FR-003` — 授权边界 | `review_writer/agent/public_entry.py::start_or_resume_review`; `review_writer/project/path_safety.py`; `review_writer/agent/fresh_bootstrap.py` | `tests/test_public_agent_entry.py` | `PARTIAL` | Fresh focused evidence covers invalid root and authorized-source mismatch as zero-write paths. Complete symlink/reparse, cross-study and public-flow evidence remains missing; Product Use, `PUBLIC_E2E` and `HUMAN_ACCEPTANCE` remain `HOLD`. |
| `FR-004` — 进度 | `review_writer/agent/public_entry.py::start_or_resume_review`; `review_writer/agent/fresh_bootstrap.py::FreshAgentBootstrap.start` | `tests/test_public_agent_entry.py` | `PARTIAL` | Fresh focused evidence covers `HUMAN_ACTION_REQUIRED`, Dashboard URL, current and revision mapping through the public caller. This is Engineering evidence only; fresh user-visible Product Use, `PUBLIC_E2E` and `HUMAN_ACCEPTANCE` remain `HOLD`. |
| `FR-005` — N-agnostic | `review_writer/agent/fresh_bootstrap.py` source-set preflight | `tests/test_fresh_bootstrap_source_set.py::test_public_fresh_bootstrap_records_all_n10_authorized_members_and_hashes`; `::test_n1_bootstrap_reaches_existing_dashboard_role_gate`; `::test_n3_bootstrap_reaches_existing_dashboard_batch_role_gate` | `PARTIAL` | Fresh public synthetic evidence: `python -m pytest -q -s tests/test_fresh_bootstrap_source_set.py::test_public_fresh_bootstrap_records_all_n10_authorized_members_and_hashes` passed (`1 passed in 9.61s`) at inspected commit `e5842ebe7f40be51d4af0d3a6e69011b329a3cd8`; `start_or_resume_review` recorded all 10 authorized members and hashes through archive preflight. `_authorized_pdfs` enumerates and deterministically sorts every legal PDF entry with no fixed upper bound. Existing N=1/N=3 dashboard-role nodes may be environment-flaky when stale Dashboard processes are present; N=20, Product Use and `PUBLIC_E2E` remain `HOLD`. |
| `FR-006` — source-set expansion | `review_writer/project/input_provenance.py`; `review_writer/project/dual_source.py` | `tests/test_fresh_bootstrap_source_set.py::test_public_fresh_bootstrap_records_all_n10_authorized_members_and_hashes` | `PARTIAL` | The fresh N=10 public run persisted the complete synthetic source set and matched every archive-preflight member/hash, providing bounded expansion evidence. Same-project add/retain/stale propagation remains unverified; N=20, Product Use, `PUBLIC_E2E` and `HUMAN_ACCEPTANCE` remain `HOLD`. |
| `FR-007` — incremental reparse | `review_writer/project/parse_reconciliation.py`; `review_writer/project/parse_quality.py` | — (no dedicated incremental-reparse test inspected) | `NOT_VERIFIED` | Reconciliation/quality seams are present, but unchanged-binding reuse, digest/provenance retention and changed-input selective reparse are not freshly verified. |
| `FR-008` — identity | `review_writer/acquisition/manifest_identity.py`; `review_writer/acquisition/supplement_identity.py` | `tests/test_fresh_bootstrap_source_set.py::test_source_role_or_stale_preflight_failure_is_zero_write` | `PARTIAL` | Identity/role preflight and a zero-write failure node are present; complete MAIN/SI, duplicate and cross-study coverage in the public flow is missing; test `NOT_RUN`. |
| `FR-009` — 默认解析器 | `review_writer/agent/local_pdf_parse.py` | — (no dedicated default-backend test inspected) | `NOT_VERIFIED` | Local parse seam is present, but fresh proof of default MinerU invocation, provenance and user-hidden internal invocation is missing. |
| `FR-010` — 真实 fallback | `review_writer/agent/local_pdf_parse.py` | — (no dedicated fallback provenance test inspected) | `NOT_VERIFIED` | Fallback-related parser seam is present, but no fresh runtime evidence proves registered fallback reason, backend/version, hashes, capability gap and retry boundary. |
| `FR-011` — Source Truth | `review_writer/project/source_truth.py`; `schemas/evidence/source_truth_bundle.v1.schema.json` | — (no dedicated Source Truth public-flow test inspected) | `PARTIAL` | Source Truth helper/schema paths exist; end-to-end source-bound locator and downstream-only reference proof is missing. |
| `FR-012` — Parse Quality | `review_writer/project/parse_quality.py`; `schemas/evidence/parse_quality_gate.v1.schema.json` | — (no dedicated Parse Quality acceptance test inspected) | `PARTIAL` | Quality-gate seam/schema exists; fresh evidence for all listed structural and MAIN/SI checks plus human decision routing is missing. |
| `FR-013` — Evidence | `review_writer/project/paper_evidence.py`; `scripts/evidence/validate_evidence_candidate.py` | `tests/test_review_evidence_ui.mjs` | `PARTIAL` | Evidence helper, validator and UI test path exist; public source-bound candidate-to-confirmed projection and scientific review evidence are missing; test `NOT_RUN`. |
| `FR-014` — Matrix/RQ binding | `review_writer/project/synthesis.py`; `review_writer/project/vertical_review.py` | `tests/test_synthesis_workspace_producer.py` | `PARTIAL` | Synthesis/workspace seams and focused test path exist; complete stable RQ/study/Evidence/locator binding and cross-study protocol proof are missing; test `NOT_RUN`. |
| `FR-015` — Gap registry | `review_writer/project/vertical_review.py` | — (no dedicated Gap-registry test inspected) | `NOT_VERIFIED` | Gap-related projection seam is present, but durable visible registry, blocking semantics and next-action evidence are not freshly verified. |
| `FR-016` — 一次性 Decision Bundle | `review_writer/agent/local_pdf_parse.py` human-action handoffs; `review_writer/project/verification_decision.py` | `tests/test_synthesis_workspace_producer.py` | `HOLD` | Internal human-action/protocol seams and sequential handoff tests exist, but one canonical Bundle, one writer transaction, idempotency and stale-revision zero-write are not evidenced; test `NOT_RUN`. |
| `FR-017` — multi-study synthesis | `review_writer/project/synthesis.py`; `review_writer/synthesis/` | `tests/test_synthesis_workspace_producer.py` | `PARTIAL` | Synthesis producer and focused tests are present; independent multi-study Product Use, preserved study boundaries and N growth/reduction evidence are missing; test `NOT_RUN`. |
| `FR-018` — PDF figure candidates | `review_writer/project/review_figures.py::project_source_figure_candidates`; `schemas/figures/source_figure.v1.schema.json` | `tests/test_review_figure_inventory_adapter.py` | `PARTIAL` | Existing focused evidence covers one authorized MAIN source, parsed locator, asset/source hashes, duplicate-asset rejection and zero-write failures. The ChemVellum inventory is read-only static only; this slice does not independently verify the source-figure adapter. Candidate-only rows do not write the registry/current; Product Use, `PUBLIC_E2E` and `HUMAN_ACCEPTANCE` remain unverified. |
| `FR-019` — attribution/license/binding | `review_writer/delivery/figure_policy.py::candidate_figure_release_gate`; `review_writer/project/review_figures.py::project_source_figure_candidates` | `tests/test_review_figure_inventory_adapter.py` | `PARTIAL` | Existing focused evidence keeps unknown rights at `HOLD` and cleared rights at `CANDIDATE_ONLY` with attribution/evidence fields. This inventory does not verify the adapter, independent rights evidence or release binding; Product Use, `PUBLIC_E2E`, `HUMAN_ACCEPTANCE`, scientific validity and promotion remain unverified. |
| `FR-020` — 草稿 | `review_writer/draft/service.py`; `review_writer/project/manuscript_v2.py` | `tests/test_review_manuscript_save_ui.mjs` | `PARTIAL` | Draft/manuscript seams and UI save test path exist; source-bound generation, protected human paragraphs and public caller evidence are missing; test `NOT_RUN`. |
| `FR-021` — Markdown/DOCX | `review_writer/delivery/project_release.py`; `review_writer/delivery/docx_integrity.py` | — (no fresh same-version export test inspected) | `PARTIAL` | Export/integrity helpers are present; same-manuscript Markdown/DOCX hash and release-lineage Product Use evidence is missing. |
| `FR-022` — stale/regenerate | `review_writer/delivery/project_release.py` | `tests/test_dashboard_runtime_root.py` | `PARTIAL` | Release/runtime seams and test path exist; complete stale-download protection and regenerate evidence in an isolated project are missing; test `NOT_RUN`. |
| `FR-023` — History | `review_writer/product_foundation/workspace_model.py`; `review_writer/draft/service.py` | `tests/test_dashboard_runtime_root.py` | `PARTIAL` | Version/history/branch seams and dashboard runtime test path exist; public compare/branch/undo/current separation evidence is missing; test `NOT_RUN`. |
| `FR-024` — cold resume | `review_writer/product_foundation/service.py`; `review_writer/agent/generator_runtime.py` | — (no dedicated cold-resume test inspected) | `NOT_VERIFIED` | Resume-related authority/runtime seams are present, but fresh process resume from the same explicit root and preservation of unfinished/user edits are not verified. |
| `FR-025` — zero-write | `review_writer/project/path_safety.py`; `review_writer/agent/fresh_bootstrap.py` | `tests/test_fresh_bootstrap_source_set.py::test_invalid_authorized_source_set_fails_before_any_project_write`; `::test_malformed_pdf_is_rejected_before_project_write` | `PARTIAL` | Focused invalid-source zero-write nodes exist; the complete error/dependency/concurrency matrix and public HTTP write-set evidence are missing; tests `NOT_RUN`. |
| `FR-026` — 安全边界 | `review_writer/project/path_safety.py`; `view/serve_review_dashboard.py` | `tests/test_dashboard_runtime_root.py` | `PARTIAL` | Path-safety/dashboard seams and runtime test path exist; complete symlink/reparse, secret-redaction and loopback PUBLIC_E2E evidence is missing; test `NOT_RUN`. |

## CR-008 traceability — ChemVellum reusable inventory

| CR | source/evidence | status | boundary |
| --- | --- | --- | --- |
| `CR-008` | `docs/THIRD_PARTY_NOTICES.md`; upstream `https://github.com/TengJiao33/ChemVellum` `main@c94ff72694cc838c19fc22359e3e0b648e2352d6`; authorization reference `USER_ATTESTED_WRITTEN_AUTHORIZATION_2026-08-21` | `PM_PROVIDED_INVENTORY`; implementation `PENDING/PARTIAL` | Read-only static inventory only. No upstream code/template/PDF copied; no open-source license is inferred from written authorization; source-figure adapter, Product Use, `PUBLIC_E2E`, `HUMAN_ACCEPTANCE`, scientific validity and `PROMOTE/B2` are unverified. |

## Evidence-layer boundary

The table is Engineering/static traceability only. `PARTIAL`, `HOLD` and `NOT_VERIFIED`
do not imply Product Use, Independent Quality, `PUBLIC_E2E`, `HUMAN_ACCEPTANCE`, scientific
validity or `PROMOTE/B2`. In particular, static seams and tests marked `NOT_RUN` cannot
cross those layers. A future update must preserve the dirty/unpinned source boundary and
replace a row only with fresh, independently attributable evidence.
