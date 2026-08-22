# Review Writer 产品包清单

本清单描述同级独立发布包 `/home/kenqia/my_folder/review-writer-product-package`。它不依赖原仓库的运行数据，也不包含任何一篇综述的 PDF、正文或临时验证记录。当前发布目录不包含 `.git`、远端配置或凭据；用户从 GitHub clone 后产生的本机 `.git` 不属于发布内容。

## 四层包

| 层 | 包内路径 | 用途 | 状态边界 |
| --- | --- | --- | --- |
| 1. 本地运行时层 | `requirements.txt`、`review_writer/` | Python 运行时、source/Evidence、VersionContext、GeneratorSession、draft、figure、release 与 DOCX 完整性 helper | 只提供能力，不保存某篇综述的 current |
| 2. Agent 合同层 | `.agents/skills/review-orchestrator/`、`qoderwork/plugins/review-writer-cn/`、`review_writer/agent/qoderwork_adapter.py`、`scripts/evidence/` | Codex 兼容 Skill、QoderWork CN Expert Kit/Skill、宿主 adapter 与确定性 evidence helpers | 宿主必须接收 topic、显式 project root、authorized PDF folder；不能自动猜 MAIN/SI |
| 3. Schema/项目权威层 | `schemas/` | Evidence、project、synthesis、figure、quality、delivery 的 JSON Schema | 每篇综述的真实数据由用户自己的 project root 持有，不随包发布 |
| 4. Dashboard/发布层 | `view/serve_review_dashboard.py`、`view/assets/dashboard/`、`skills/review-export-docx/scripts/md2docx.py`、`skills/review-export-docx/review_template.docx` | 本地 Dashboard、人工决策/编辑/History、同版本 Markdown/DOCX 导出 | DOCX 模板是唯一随包二进制例外；`latex2word` 仍是可选增强 |

## 包含

- 包根的受控集合只有：`.gitignore`、`.env.example`、`README.md`、`docs-qoderwork-cn.md`、`product-package-manifest.md`、`product-package-sha256.txt`、`requirements.txt`、`docs/PRODUCT_CONTRACT.md`、`docs/PRODUCT_TRACEABILITY.md`、`docs/THIRD_PARTY_NOTICES.md`、`review_writer/`、`view/`、`schemas/`、`scripts/evidence/`、`scripts/windows/`、`scripts/build_qoderwork_plugin_zip.py`、`.agents/skills/review-orchestrator/`、`qoderwork/plugins/review-writer-cn/`、`skills/mineru-precise-parse-review-writer/` 与 `skills/review-export-docx/`。
- `review_writer/` 全部 Python 源码（排除 `__pycache__`）。
- `view/serve_review_dashboard.py` 与 `view/assets/dashboard/` 的全部页面、JS、CSS。
- `schemas/` 的全部 JSON Schema。
- `scripts/evidence/` 的全部 runtime-imported helper。
- `scripts/windows/Install-ReviewWriter.ps1` 和 `Test-ReviewWriterEnvironment.ps1`：Windows
  用户的一次性安装与只读诊断入口；它们不读取或打印凭据。
- `skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py`：官方
  MinerU v4 API 的本地 PDF adapter；只在外部 token 可用时联网，输出现有
  `local_pdf_parse` 所要求的 manifest/content-list/layout/Markdown 结构。
- project-local `.agents/skills/review-orchestrator/SKILL.md` 与 `agents/openai.yaml`。
- QoderWork CN Expert Kit：`qoderwork/plugins/review-writer-cn/.qoder-plugin/plugin.json`、`qoderwork.md`、`skills/review-writer/SKILL.md` 与插件 README。
- `review_writer/agent/qoderwork_adapter.py`：只调用现有 `FreshAgentBootstrap` 与 `VersionContext`，不创建第二套 authority。
- `scripts/build_qoderwork_plugin_zip.py`：构建并检查不含凭据/本机状态的确定性插件 ZIP。
- 运行时直接解析的 DOCX helper：`md2docx.py` 和 `review_template.docx`。
- 本说明、`README.md`、`docs/PRODUCT_CONTRACT.md`、`docs/PRODUCT_TRACEABILITY.md`、`docs/THIRD_PARTY_NOTICES.md`、`requirements.txt` 和最终 `product-package-sha256.txt`。
- `.env.example`：仅记录实际支持的 parser 路径、MinerU CA bundle 变量和 QoderWork 凭据边界，不含任何值。
- package-local `.gitignore`：忽略 venv、bytecode、`.env*`（显式保留 `.env.example`）、project/review data、PDF/DOCX/ZIP、日志、缓存与 OS/IDE 文件；运行必需的 `skills/review-export-docx/review_template.docx` 是唯一 DOCX 豁免。

## CR-011 Windows/QoderWork CN environment preparation

本切片的受控新增集合是 `.env.example`、`scripts/windows/Install-ReviewWriter.ps1`、
`scripts/windows/Test-ReviewWriterEnvironment.ps1` 及对应 Windows/QoderWork 文档、
traceability、third-party notice 和 focused static tests。安装器只创建或复用产品包本地
`.venv`、安装 `requirements.txt` 并构建既有 Expert Kit ZIP；诊断器只读检查本地依赖、
`pdftotext`、插件布局以及 `REVIEW_WRITER_MINERU_PARSER`，不读取或打印 secret。它们不写入
用户 review project root、不自动下载 Poppler、不安装 QoderWork 客户端，也不替代
Dashboard 的人工闸门。PowerShell 未在本 Linux/WSL 工具环境中执行，真实 QoderWork UI、
Product Use、`PUBLIC_E2E`、`HUMAN_ACCEPTANCE`、scientific validity 和 `PROMOTE/B2` 仍为
`HOLD`；回滚边界为一次 Git revert，或仅移除本次新建的 `.venv`/`build` 目录。

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
- MinerU token/config、外部 parser 的凭据和开发库的旧 global skills；本包现在包含一个
  适配官方 MinerU v4 API 的 parser，但不包含任何 token。若凭据或网络不可用，沿用真实
  `pdftotext` fallback。
- `.env`、`.env.production`、auth/token/cookie/session、私钥、真实 URL 或任何未脱敏的本地路径。

源树已按下列精确目录排除而非“复制后清理”：`tests/`、`.worktrees/`、`.playwright-mcp/`、`.gstack/`、`.codex/`、`.ai/`、`.superpowers/`、`.agent-orchestration-runs/`、`projects/`、`review-projects/`、`chem_papers/`、`docs/superpowers/`、`docs/handoff/`、旧 `skills/` 子目录和 `view/assets/demo/`。源根的 `AGENTS.md`、`Makefile`、内部 CLI 入口 `scripts/run_vertical_review.py`、控制台/网络/会话记录均未复制；QoderWork CN 的用户插件与 adapter 是本产品包的显式用户入口，不属于开发控制面。

## 安装依赖依据

实际 import 审计确认：`jsonschema` 用于 schema 校验，`Pillow` 用于图件策略与比较图，`python-docx` 用于 DOCX helper，`requests` 用于 MinerU v4 API adapter，`truststore` 在 Windows 上接入系统 CA。`latex2word` 仅在存在时启用数学 OMML 排版，因此没有列为硬依赖。`pdftotext` 是本地 PDF fallback 所需的系统命令；Agent 会在需要时 fail-closed，不会联网下载或猜测来源。

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

## CR-013 Windows source-relative path portability

本维护切片仅更新 `review_writer/project/source_truth.py`、
`view/serve_review_dashboard.py`、对应 focused regression test、traceability 和 notice；不新增
依赖、不改变 Source Truth/VersionContext/Dashboard authority，也不把 Windows 绝对路径变成可写入
的 project-relative path。外部相对路径统一以 canonical POSIX 形式比较和序列化；绝对路径、遍历、
reparse/link 安全边界继续 fail-closed。WSL focused evidence 不等同于 Windows native、QoderWork、
Product Use、`PUBLIC_E2E` 或 `HUMAN_ACCEPTANCE`，回滚边界为一次 Git revert。
