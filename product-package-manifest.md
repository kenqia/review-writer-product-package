# Review Writer 产品包清单

本清单描述同级独立发布包 `/home/kenqia/my_folder/review-writer-product-package`。它不依赖原仓库的运行数据，也不包含任何一篇综述的 PDF、正文或临时验证记录。当前发布目录不包含 `.git`、远端配置或凭据；用户从 GitHub clone 后产生的本机 `.git` 不属于发布内容。

## 四层包

| 层 | 包内路径 | 用途 | 状态边界 |
| --- | --- | --- | --- |
| 1. 本地运行时层 | `requirements.txt`、`review_writer/` | Python 运行时、source/Evidence、VersionContext、GeneratorSession、draft、figure、release 与 DOCX 完整性 helper | 只提供能力，不保存某篇综述的 current |
| 2. Agent 合同层 | `.agents/skills/review-orchestrator/`、`scripts/evidence/` | Codex 可发现的 source-bound Skill 与被 runtime 导入的确定性 evidence helpers | Agent 必须接收 topic、显式 project root、authorized PDF folder；不能自动猜 MAIN/SI |
| 3. Schema/项目权威层 | `schemas/` | Evidence、project、synthesis、figure、quality、delivery 的 JSON Schema | 每篇综述的真实数据由用户自己的 project root 持有，不随包发布 |
| 4. Dashboard/发布层 | `view/serve_review_dashboard.py`、`view/assets/dashboard/`、`skills/review-export-docx/scripts/md2docx.py`、`skills/review-export-docx/review_template.docx` | 本地 Dashboard、人工决策/编辑/History、同版本 Markdown/DOCX 导出 | DOCX 模板是唯一随包二进制例外；`latex2word` 仍是可选增强 |

## 包含

- 包根的受控集合只有：`.gitignore`、`README.md`、`product-package-manifest.md`、`product-package-sha256.txt`、`requirements.txt`、`docs/PRODUCT_CONTRACT.md`、`docs/PRODUCT_TRACEABILITY.md`、`docs/THIRD_PARTY_NOTICES.md`、`review_writer/`、`view/`、`schemas/`、`scripts/evidence/`、`.agents/skills/review-orchestrator/` 与 `skills/review-export-docx/`。
- `review_writer/` 全部 Python 源码（排除 `__pycache__`）。
- `view/serve_review_dashboard.py` 与 `view/assets/dashboard/` 的全部页面、JS、CSS。
- `schemas/` 的全部 JSON Schema。
- `scripts/evidence/` 的全部 runtime-imported helper。
- project-local `.agents/skills/review-orchestrator/SKILL.md` 与 `agents/openai.yaml`。
- 运行时直接解析的 DOCX helper：`md2docx.py` 和 `review_template.docx`。
- 本说明、`README.md`、`docs/PRODUCT_CONTRACT.md`、`docs/PRODUCT_TRACEABILITY.md`、`docs/THIRD_PARTY_NOTICES.md`、`requirements.txt` 和最终 `product-package-sha256.txt`。
- package-local `.gitignore`：忽略 venv、bytecode、`.env*`（显式保留 `.env.example`）、project/review data、PDF/DOCX/ZIP、日志、缓存与 OS/IDE 文件；运行必需的 `skills/review-export-docx/review_template.docx` 是唯一 DOCX 豁免。

示例（均为脱敏格式，不指向真实数据）：

```text
project_root: /home/researcher/review-projects/nickel-coupling-review
authorized_pdf_folder: /home/researcher/authorized-pdfs/nickel-coupling
project_id: nickel-coupling-review
dashboard_route: http://127.0.0.1:<agent-selected-port>/review?project_id=<project-id>
current_pointer: .review-writer/version_context/current.json
```

## 排除

- `tests/`、`.worktrees/`、`.playwright-mcp/`、`.gstack/`、`.codex/`、`.ai/`、`.superpowers/`、`.agent-orchestration-runs/`。
- `projects/`、`review-projects/`、`chem_papers/` 以及任何 review data、source archive、PDF、截图、DOCX 结果、日志或临时 evidence。
- `docs/superpowers/`、`docs/handoff/`、`AGENTS.md`、`Makefile`、内部 CLI 文档和演示页面。
- 旧 global skills、MinerU token/config 与外部 parser；本包只保留 project-local `review-orchestrator` 和必要 DOCX helper。缺少外部 MinerU 时沿用本地 `pdftotext` fallback。
- `.env`、`.env.production`、auth/token/cookie/session、私钥、真实 URL 或任何未脱敏的本地路径。

源树已按下列精确目录排除而非“复制后清理”：`tests/`、`.worktrees/`、`.playwright-mcp/`、`.gstack/`、`.codex/`、`.ai/`、`.superpowers/`、`.agent-orchestration-runs/`、`projects/`、`review-projects/`、`chem_papers/`、`docs/superpowers/`、`docs/handoff/`、旧 `skills/` 子目录和 `view/assets/demo/`。源根的 `AGENTS.md`、`Makefile`、内部 CLI 入口 `scripts/run_vertical_review.py`、原始 README、控制台/网络/会话记录均未复制。

## 安装依赖依据

实际 import 审计确认：`jsonschema` 用于 schema 校验，`Pillow` 用于图件策略与比较图，`python-docx` 用于 DOCX helper。`latex2word` 仅在存在时启用数学 OMML 排版，因此没有列为硬依赖。`pdftotext` 是本地 PDF fallback 所需的系统命令；Agent 会在需要时 fail-closed，不会联网下载或猜测来源。

## 质量边界

合同文档和 FR 追踪表是公开规范边界；它们不复制 dirty/unpinned 的源 SRS/Design
正文。本次已按上述受控 allowlist 对包内所有受控普通文件全量重算
`product-package-sha256.txt`（排除 hash 文件自身、`.git`、tests、cache/pyc 及用户项目/数据），
并验证 hash 条目与受控普通文件 exact set 一致、所有 digest 复算一致。该完整性检查不升级
`Product Use`、`PUBLIC_E2E`、`HUMAN_ACCEPTANCE`、scientific validity 或 `PROMOTE/B2` 边界。

- `Engineering`：import、静态资产和针对性运行时检查；不代表用户验收。
- `Independent Quality`：需要独立浏览器与新鲜环境的验证，不随本包 hash 推断。
- `Product Use`：需要在隔离 project root 中走 Evidence、Draft、DOCX、stale/regenerate、History、cold resume。
- `PUBLIC_E2E`、`HUMAN_ACCEPTANCE`、scientific validity、`PROMOTE/B2`：必须由独立真实流程和研究者决定；本包不自动宣称通过。

发布包 hash 文件只覆盖包内普通文件（不把自身列入自身的 hash 输入）；重新复制或修改任一文件后应重新生成 hash 文件。
