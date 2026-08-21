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
| MinerU precise parse | `skills/mineru-precise-parse-chemvellum/` | `53b577be3a617499043f4f11b1204a3721f22558` | 只读借鉴本地 PDF → Markdown 的 batch/provenance 边界；不上传或复制本地 PDF，不复制 token/config。 | `review_writer/agent/local_pdf_parse.py` 与 `.agents/skills/review-orchestrator/SKILL.md`；解析输出仍须经过 parse quality、human decision 与 source binding。 |

## Verification status and limits

本次核验仅为 upstream URL、`main` commit、路径和 commit history 的只读静态检查。
没有复制或执行 upstream 代码，没有复制模板、PDF、素材、metadata、查询结果或
授权书原文；source-figure adapter 尚未验证。当前记录不声称以下任何一项已经通过：

- review-writer Product Use、`PUBLIC_E2E`、`HUMAN_ACCEPTANCE`；
- Independent Quality、scientific validity、release 或 `PROMOTE/B2`；
- 任一组件的最终 license/rights clearance 或可直接发布资格。

本 inventory 不缩减 review-writer 的最终产品 scope（包括 FR-001..FR-026 及其他
已批准合同）；组件复用只能在现有 source/Evidence、人工决策、release 和 promotion
边界内逐项实现和验证。
