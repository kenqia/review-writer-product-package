# Third-party notices

## 记录范围与授权边界

本文件登记 review-writer 可能复用的 ChemVellum reusable-components inventory。
它是 PM 提供的来源与适用边界记录，不是本次产品代码变更，也不表示任何组件已经
通过 Product Use、`PUBLIC_E2E` 或人类验收。

- upstream：<https://github.com/TengJiao33/ChemVellum>
- upstream `main` commit：`c94ff72694cc838c19fc22359e3e0b648e2352d6`
- user-provided written authorization reference：`USER_ATTESTED_WRITTEN_AUTHORIZATION_2026-08-21`
- 本文件不记录授权书私人原文、授权人身份、token、cookie 或其他私密信息。

截至上述 commit，upstream `main` 没有 `LICENSE` 文件，也没有公开 SPDX/OSI license
声明。书面授权是本次复用记录的依据，但不是开源许可证；每项复用仍须遵守该授权
范围和相应的 attribution/rights review，未知权利不得进入 release。

## ChemVellum reusable inventory

下表记录精确 upstream 路径、来源 commit、拟复用方式与 review-writer 落点。这里的
“落点”是本包既有 seam 或拟绑定的责任边界；本次没有复制 upstream 代码、模板或
PDF，也没有新增对 ChemVellum 的运行时依赖。

| 组件 | upstream 精确路径 | 来源 commit | 复用方式 | review-writer 落点 |
| --- | --- | --- | --- | --- |
| Paper figure inventory builder | `skills/review-source-figure-tools/scripts/build_paper_figure_inventory.py` | `53b577be3a617499043f4f11b1204a3721f22558` | 只读借鉴从解析产物发现图件、保留 locator/hash/rights hint 的字段语义；不复制 builder。 | `review_writer/project/review_figures.py::project_source_figure_candidates`；输出仍是 candidate-only，registry/current 不由该 adapter 写入。 |
| Asset insertion | `skills/review-citation-assets/scripts/insert_assets.py` | 文件引入：`5aed49a0521bc2d92ff97e1ce3900b24c24c03fc`；目录后续更新：`524e5f2b8094aee1b236153501302025af5e9f4d` | 只读借鉴显式 marker、asset hash、attribution 与机械错误报告；不复制脚本或素材。 | `review_writer/delivery/project_release.py`、`review_writer/delivery/figure_policy.py`；发布前继续执行显式 source-figure policy。 |
| Citation merge | `skills/review-citation-assets/scripts/merge_citations.py` | `524e5f2b8094aee1b236153501302025af5e9f4d` | 只读借鉴稳定 citation/reference 合并和 fail-closed 输入检查；不复制脚本。 | `review_writer/delivery/project_release.py` 的 manuscript/reference 校验与 `review_writer/delivery/docx_integrity.py` 的导出完整性检查。 |
| Markdown to DOCX | `skills/review-export-docx/scripts/md2docx.py` | `524e5f2b8094aee1b236153501302025af5e9f4d` | 在 written authorization 范围内登记为可复用的导出实现参考；本次不复制模板或脚本。 | 包内既有 `skills/review-export-docx/scripts/md2docx.py` 与 `review_writer/delivery/project_release.py` 的 same-manuscript DOCX 责任边界。 |
| DOCX audit | `skills/review-export-docx/scripts/audit_docx.py` | `5aed49a0521bc2d92ff97e1ce3900b24c24c03fc` | 只读借鉴 DOCX 结构、媒体与 attribution 审计职责；不复制脚本。 | `review_writer/delivery/docx_integrity.py`、`review_writer/delivery/project_release.py`；审计结果不能替代 Product Use。 |
| DOCX render | `skills/review-export-docx/scripts/render_docx.py` | `524e5f2b8094aee1b236153501302025af5e9f4d` | 只读借鉴导出后 render/visual check 的验证责任；不复制渲染产物。 | `skills/review-export-docx/scripts/md2docx.py` 的导出边界及独立质量验证层；不把静态 render 记录当作验收。 |
| Topic paper discovery | `skills/review-topic-paper-discovery/` | `ced2cf7799adadcd895e5e986f2879866cd6319d` | 只读借鉴 topic → candidate corpus → lawful full text 的流程契约；不复制 skill、查询结果或论文。 | `.agents/skills/review-orchestrator/SKILL.md`、`review_writer/agent/public_entry.py` 与本地 source-bound project root；Agent 仍须接收显式授权目录。 |
| Review metadata prep | `skills/review-metadata-prep/` | `524e5f2b8094aee1b236153501302025af5e9f4d` | 只读借鉴 metadata registry、path validation 与 repair 的职责；不复制 metadata 或论文。 | `review_writer/acquisition/reusable_library.py`、`review_writer/project/input_provenance.py` 与 project-local Evidence/VersionContext。 |
| MinerU precise parse | `skills/mineru-precise-parse-chemvellum/`; exact parser `skills/mineru-precise-parse-chemvellum/scripts/parse_chemvellum_pdfs.py` | repository `main@c94ff72694cc838c19fc22359e3e0b648e2352d6`; skill blob `4da32173a2dbd948bc3f22977caf53726c4b719f`; parser blob `530cdaf15977b44d72efe820852bd5d881c06fd7` | 只读借鉴本地 PDF → Markdown 的 batch/provenance 边界；当前不复制或 vendoring parser、token/config 或本地 PDF。由于 upstream 文件名、CLI 和输出布局不同，clean-room adapter 仍为 `REFERENCE_ONLY / ADAPTER_HOLD`。 | `review_writer/agent/local_pdf_parse.py` 的现有 `parse_review_writer_pdfs.py` resolver、SourceTruth、parse-quality、VersionContext；解析输出仍须经过 parse quality、human decision 与 source binding。 |

## CR-008 component implementation record — Paper figure inventory builder

本记录只覆盖一个组件；没有 vendoring 上游脚本、模板、PDF、图片或引入运行时依赖。

- upstream URL：<https://github.com/TengJiao33/ChemVellum>
- pinned upstream repository commit：`c94ff72694cc838c19fc22359e3e0b648e2352d6`
- exact upstream file：`skills/review-source-figure-tools/scripts/build_paper_figure_inventory.py`
- exact file-content commit：`53b577be3a617499043f4f11b1204a3721f22558`
- authorization reference：`USER_ATTESTED_WRITTEN_AUTHORIZATION_2026-08-21`
- upstream license：GitHub `license=null`；书面授权不被推断为开源许可证。

### State and authority mapping

```text
PLANNED
→ ADAPTED
→ PUBLIC_CALLER_CONNECTED
→ REAL_RELEASE_CONSUMED
→ VERIFIED (bounded Engineering/public-caller evidence)
→ PROMOTED: HOLD
```

Fresh evidence in this component commit reaches `ADAPTED`, `PUBLIC_CALLER_CONNECTED`,
`REAL_RELEASE_CONSUMED` and bounded `VERIFIED`. `PROMOTED` remains `HOLD` pending the
complete Agent-first N=3 public flow, Product Use, `PUBLIC_E2E`, `HUMAN_ACCEPTANCE` and
scientific validity. The status is not a claim that the upstream component or the overall
figure FRs are complete.

### Clean-room adapter and canonical authorities

- adapter：`review_writer/project/figure_inventory_adapter.py`；只移植确定性语义：
  whitespace/caption/label 清洗、source-order key、已分组 fragment 保留、source-bound
  Markdown license/rights hint、PDF/asset SHA-256 与 page/bbox 输入校验。
- canonical candidate seam：
  `review_writer/project/review_figures.py::project_source_figure_candidates`。
- Agent caller：
  `review_writer/agent/local_pdf_parse.py::_build_staged_figure_candidates` →
  `project_source_figure_candidates`；registry producer 仍是
  `build_source_figure_registry`，adapter 只读投影其 fragments。
- Dashboard caller：现有 `/review-figures` candidate projection and
  `write_project_workspace_decision`；human rights overlay remains the only rights
  clearing path。
- release caller：现有 `materialize_source_figure_registry` →
  `review_writer/delivery/project_release.py::build_project_release` →
  `figure_validation` and Markdown/DOCX release snapshot。
- canonical authority mapping：candidate remains Agent/VersionContext snapshot data;
  selected/cleared rows remain `source_figure_registry`; manuscript current/history,
  VersionContext and release snapshot remain existing authorities. No second
  SourceRecord/Evidence, figure registry, manuscript state or release state is created。

### Write set, failure and rollback boundary

This component commit writes only:

```text
review_writer/project/figure_inventory_adapter.py
review_writer/project/review_figures.py
review_writer/agent/local_pdf_parse.py
tests/test_review_figure_inventory_adapter.py
docs/THIRD_PARTY_NOTICES.md
docs/PRODUCT_TRACEABILITY.md
```

Candidate enrichment is read-only: malformed source/role/hash/locator/asset/fragment or
drifted canonical Markdown fails before candidate publication and leaves candidate files,
registry, current/history and VersionContext unchanged. Human materialization and release
continue to use their existing atomic writes and rollback; a failed release restores the
existing Markdown/DOCX/snapshot/quality targets. The rollback boundary for this component
is reverting this one commit; no upstream files are modified。

### Focused evidence and limits

Fresh Engineering evidence is `13 passed` in
`tests/test_review_figure_inventory_adapter.py`, including clean text, deterministic
source order, grouped fragments/page/bbox/hash provenance, rights-hint-only behavior,
bad Markdown hash zero-write, and a real `build_project_release` consumer that writes a
same-version Markdown/DOCX pair and checks release `figure_validation`. The adjacent
Agent/Dashboard figure bridge and materialization tests are `8 passed`。

The release consumer uses the real producer and converter while isolating unrelated
workflow/docx/schema readiness gates; it is bounded Engineering evidence, not a claim of
full Agent-first N=3 Product Use. A rights hint never clears rights, and this commit does
not claim Product Use, complete `PUBLIC_E2E`, `HUMAN_ACCEPTANCE`, scientific validity or
`PROMOTE/B2`。

## CR-008 component implementation record — ChemVellum MinerU parser (`REFERENCE_ONLY / ADAPTER_HOLD`)

- upstream URL：<https://github.com/TengJiao33/ChemVellum>
- pinned upstream repository commit：`c94ff72694cc838c19fc22359e3e0b648e2352d6`
- exact skill path：`skills/mineru-precise-parse-chemvellum/`；skill blob：`4da32173a2dbd948bc3f22977caf53726c4b719f`
- exact parser path：`skills/mineru-precise-parse-chemvellum/scripts/parse_chemvellum_pdfs.py`；parser blob：`530cdaf15977b44d72efe820852bd5d881c06fd7`
- authorization reference：`USER_ATTESTED_WRITTEN_AUTHORIZATION_2026-08-21`
- upstream license：GitHub `license=null`；书面授权不被推断为开源许可证。

### Reuse decision and authority boundary

The upstream file is a batch parser with a different filename (`parse_chemvellum_pdfs.py`),
CLI contract, input defaults and output layout from review-writer's existing
`parse_review_writer_pdfs.py` seam. It also resolves token/config inputs that must remain
outside the product package. Therefore this slice records behavior and contract facts only;
it does not copy, vendor, execute or import the upstream script, token/config, local PDF,
template or output artifact.

The review-writer implementation remains the sole parser caller and authority:

```text
public_entry.start_or_resume_review
  -> local_pdf_parse._resolve_mineru_parser
  -> existing review-writer parser CLI (if installed)
  -> SourceTruth + parse-quality + VersionContext
  -> existing Dashboard human gate
```

The current clean-room adapter status is `REFERENCE_ONLY / ADAPTER_HOLD`: the portability
slice has fresh `tests/test_mineru_parser_portability.py` evidence for the review-writer
parser path (`6 passed`), but that is not evidence that the ChemVellum batch script itself
is connected or that any release consumes it. A future adapter must first prove the exact
CLI/output contract, preserve input/output hashes and capability/chemical gaps, reject
stale or malformed output before publication, and feed only the existing SourceTruth and
VersionContext chain. That requires a new implementation slice and focused public-caller
test; no second parser registry, evidence authority or release writer is permitted.

### Write set, failure and rollback boundary

This record writes only this notice and the corresponding traceability row. It writes no
project evidence, parser output, token/config, SourceTruth, VersionContext, manuscript or
release artifact. Reverting the documentation commit removes the record and changes no
review project. Until the adapter is separately approved and verified, `REAL_RELEASE_CONSUMED`,
Product Use, `PUBLIC_E2E`, `HUMAN_ACCEPTANCE`, scientific validity and `PROMOTE/B2` remain
`HOLD`.

## CR-008 component implementation record — Asset insertion (`HOLD`)

- upstream URL：<https://github.com/TengJiao33/ChemVellum>
- pinned upstream repository commit：`c94ff72694cc838c19fc22359e3e0b648e2352d6`
- exact upstream file：`skills/review-citation-assets/scripts/insert_assets.py`
- file-content/introduced commit：`5aed49a0521bc2d92ff97e1ce3900b24c24c03fc`
- later directory context：`524e5f2b8094aee1b236153501302025af5e9f4d`
- authorization reference：`USER_ATTESTED_WRITTEN_AUTHORIZATION_2026-08-21`
- upstream license：GitHub `license=null`；书面授权不被推断为开源许可证。

### HOLD rationale and authority boundary

The upstream behavior is useful as a review reference: allowed asset kinds, unique
`asset_id`, source-paper locator/attribution/reuse basis, exactly-one insertion marker,
asset hash verification, mechanical error reporting, zero-write on validation failure,
and temporary-file replacement. This package already has the canonical equivalent in
`review_writer/delivery/figure_policy.py::validate_new_route_figure_policy`,
`review_writer/project/review_figures.py::validate_source_figure_target_binding`, and
`review_writer/delivery/project_release.py::build_project_release`. Those seams validate
the current source-figure registry, target binding, asset hash, attribution, rights and
same-version Markdown/DOCX release. They are consumed by the real Dashboard/release
caller and own the only project write set.

No independent `insert_assets` adapter is added because it would introduce a second
asset manifest, insertion writer, or release authority. The component therefore remains
`HOLD`, not `ADAPTED`, `PUBLIC_CALLER_CONNECTED`, `REAL_RELEASE_CONSUMED`, or `VERIFIED`.
The current equivalent seams have focused evidence in
`tests/test_release_source_figure_provenance.py`,
`tests/test_project_release_version_binding.py`, and the figure inventory/bridge suites;
that evidence is not attributed to the upstream component and does not promote this
component. No upstream code, template, image or script was copied.

### Write set and rollback boundary

This HOLD record writes only this notice and its traceability row. It writes no project
files, registry, manuscript, release artifact or VersionContext. Reverting this one
documentation commit removes the record and changes no review project. Any future
adapter would first need a new change request proving a distinct canonical seam,
zero-write/atomic rollback behavior, focused test, and a real Dashboard/release consumer.

## Verification status and limits

初始 inventory 记录是 upstream URL、`main` commit、路径和 commit history 的只读静态检查。
本组件提交没有复制或执行 upstream 代码，没有复制模板、PDF、素材、metadata、查询
结果或授权书原文；新增 clean-room adapter 仅按上面的 bounded focused evidence 验证。
当前记录不声称以下任何一项已经通过：

- review-writer Product Use、`PUBLIC_E2E`、`HUMAN_ACCEPTANCE`；
- Independent Quality、scientific validity、release 或 `PROMOTE/B2`；
- 任一组件的最终 license/rights clearance 或可直接发布资格；本组件的 Markdown
  license hint 仍不能替代 researcher rights decision。

本 inventory 不缩减 review-writer 的最终产品 scope（包括 FR-001..FR-026 及其他
已批准合同）；组件复用只能在现有 source/Evidence、人工决策、release 和 promotion
边界内逐项实现和验证。
