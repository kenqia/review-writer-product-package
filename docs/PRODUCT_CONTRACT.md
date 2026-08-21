# Review Writer 公开产品合同

## 合同声明

本文件与 [`PRODUCT_TRACEABILITY.md`](PRODUCT_TRACEABILITY.md) 构成
`review-writer-product-package` 的公开 canonical contract。它们规定公开
product-package 的当前规范边界；README 只提供面向用户的入口和验证边界。

```text
contract_version: review-writer-public-contract/2026-08-21
effective_date: 2026-08-21
canonical_contract: review-writer-product-package (this document + PRODUCT_TRACEABILITY.md)
```

`contract_version` 是本次公开合同声明新定义的稳定标识，不冒充源仓库已有版本。

合同引用的 source checkout 是只读检查得到的参考，不是本包的一部分：

| 文档 | source checkout | source relative path | source status | SHA-256 | 复制正文 |
| --- | --- | --- | --- | --- | --- |
| SRS | `/home/kenqia/my_folder/review-writer` | `docs/product/REVIEW_WRITER_SRS.md` | `DIRTY_UNPINNED` | `4b24c66c77af15c19a9b83774d5886a43d85d11ddaa01ea98556a8a3979891e2` | `No` |
| Design | `/home/kenqia/my_folder/review-writer` | `docs/product/REVIEW_WRITER_DESIGN.md` | `DIRTY_UNPINNED` | `2eef3720831460f58b7ab0c591205fbff62ee50a60c1e80448ad79b2b4f77cf9` | `No` |

源 SRS/Design 是 dirty、未 commit-pinned 的 source-of-truth reference；上表的
SHA-256 只绑定本次读取到的文件字节，不伪造 commit 版本。公开包不复制正文。
因此，当源文档和公开合同边界不一致时，公开 package 文档是对外可依赖的规范
边界；需要改变实现仍必须通过独立的代码变更和验证，不由本声明隐式批准。

## 公开入口与状态边界

唯一可发现的公开入口合同是：

```text
start_or_resume_review(
    topic,
    explicit_project_root,
    authorized_pdf_folder,
    rq=None,
    scope=None,
    output_format=None,
)
```

Agent 接收自然语言 topic、显式 project root 和获授权 PDF folder；缺少必填项
时返回可理解的缺口，不让用户运行内部命令。`fresh` 从空 project root 建立
项目；`resume` 复用同一 explicit project root，并读取其中的 current、revision、
VersionContext 和未完成决定。公开响应至少暴露 `dashboard_url`、`current`、
`revision`、`status` 与 `write_mode`。需要研究者决定时返回
`HUMAN_ACTION_REQUIRED` 并停在 Dashboard；入口、public caller 或上述响应字段
缺失时，合同状态为 `HOLD`。

项目 `.review-writer/version_context` 是 sources、Evidence、正文、图件、release、
current 和版本的 durable authority。Dashboard 是人类决策面，不是第二事实源。
合同和验证必须分开报告 Engineering、Independent Quality、Product Use、
`PUBLIC_E2E`、`HUMAN_ACCEPTANCE`、scientific validity 与 `PROMOTE/B2`；静态代码
路径或未运行的测试不能跨越这些层级。

## 已批准合同变更

### CR-001 — 可发现的 Agent-first 入口

冻结公开 `start_or_resume_review(topic, explicit_project_root,
authorized_pdf_folder, …)` 合同，覆盖 fresh/resume、Dashboard URL、current、
revision、status 和 write mode。它是公开调用方的入口边界；缺少可发现的公开
caller 或这些响应字段保持 `HOLD`，不以内部 helper 名称替代。

### CR-002 — milestones-only 交付排序

CR-002 只允许把交付拆成 milestones，不缩减最终产品范围，也不把 milestone 当作
新的状态机或第二 authority。可采用如下排序：

```text
PUBLIC_INTAKE_V0 -> SOURCE_SET_V1 -> SYNTHESIS_V2 -> RELEASE_V3
```

这只是 delivery sequencing；FR-001..FR-026 和其余批准合同仍是最终 scope。

### CR-003 — public caller 与人工动作边界

公开 caller 负责把自然语言请求映射到入口合同、返回项目状态和 Dashboard URL。
MAIN/SI identity、parse quality、Evidence、冲突、权利和正文批准等需要人类判断的
事项必须返回 `HUMAN_ACTION_REQUIRED`。缺失、错误角色、过期或冲突输入必须
fail-closed；Agent 不得把候选、静态检查或自身文本当作人类批准。

### CR-004 — `02_claims` / `02_synthesis` canonical boundary

批准的边界是：`02_claims` 保存 source-bound、per-study 的 Source Truth/Evidence
及其 claim projection；`02_synthesis` 只消费这些 claims，并保存跨 study 的
Comparison Protocol、Coverage、Synthesis Claim 和 Section Contract。`02_synthesis`
不得把 legacy matrix 当作事实源，也不得反向写入 source-bound claims。

这是合同方向的批准，不是代码已经满足的证明；当前 public seam 尚未独立验证时，
实现保持 `HOLD`，不得借本文件静默改写代码或数据边界。

### CR-005 — 一个 Decision Bundle public surface

公开面只提供一个 Decision Bundle，复用现有 internal evidence、protocol、synthesis
和 section seams。它聚合 source identity、parse provenance、Evidence/Matrix/Gap/
figure/section/release 影响、决定选项、理由、预计写入集合、revision 和冲突提示。
不得新增第二套 state machine、current/history、marker、receipt 或 lease；Bundle
是人工事项的聚合，不是自动批准器。

### CR-006 — 稳定 HTTP、错误和写入合同

公开错误响应使用稳定类别、错误码、最小修复动作，并明确 current 是否改变和
`write_mode`。意向合同如下；这是预期边界，不是运行时证明：

| 情形 | HTTP | `error.category` | `write_mode` | current | retry action |
| --- | ---: | --- | --- | --- | --- |
| 缺失/格式错误/未授权路径/错误角色/重复输入 | `400` | `INVALID_INPUT` | `zero_write` | unchanged | 修正输入后重新提交 |
| stale revision、旧 current、并发冲突或重复 revision/digest | `409` | `VERSION_CONFLICT` | `zero_write` | unchanged | 重新读取 current/Revision，再从新 Bundle 重试 |
| 合法但依赖或人工决定不完整 | `400` 或合同定义的阻断响应 | `PRECONDITION_FAILED` | `candidate_only` 或 `zero_write` | unchanged | 完成缺口或人工决定 |

错误 body 不回显 secret、token、cookie、auth 或完整敏感日志。成功写入遵循
pointer-last：先完成并校验 candidate/immutable decision，再最后更新 current
pointer；任一前置校验失败不得移动 current。当前 public HTTP caller、状态码和
zero-write 行为未经过本合同提交的运行时测试，不宣称已证实。

### CR-007 — 状态与证据分类

合同允许的实现状态是：

| 状态 | 含义 |
| --- | --- |
| `IMPLEMENTED` | 有可定位实现、公开调用方、针对性 fresh evidence，且证据只覆盖声明的层级 |
| `PARTIAL` | 有部分 seam/evidence，但缺少完整 public caller、持久化、失败路径或层级证据 |
| `HOLD` | 已识别的硬阻断，继续声称完成会越过合同或安全边界 |
| `NOT_VERIFIED` | 可能存在 seam，但本次没有足够 fresh evidence 证明合同行为 |
| `PENDING_SOURCE_CONFIRMATION` | 尚未完成源文档或 FR 绑定确认；本合同声明不补猜测 |

这些状态必须与证据层级一起读取。Engineering/Independent Quality 证据不能替代
Product Use；Product Use 不能替代 `PUBLIC_E2E` 或 `HUMAN_ACCEPTANCE`；任何静态
证据都不能替代 scientific validity 或 `PROMOTE/B2`。

### CR-008 — canonical reusable-components inventory

CR-008 已由 PM 提供 inventory，完整记录见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。当前 inventory 绑定
ChemVellum upstream `https://github.com/TengJiao33/ChemVellum` 的 `main`
commit `c94ff72694cc838c19fc22359e3e0b648e2352d6`，并使用书面授权引用
`USER_ATTESTED_WRITTEN_AUTHORIZATION_2026-08-21`。upstream `main` 无 LICENSE、
公开 SPDX/OSI license；书面授权不是开源许可证。

```text
inventory_status: PM_PROVIDED_INVENTORY
implementation_status: PENDING/PARTIAL
```

这只是来源、复用方式、责任落点和限制的合同登记，不是代码复制、运行时依赖或
权利批准。source-figure adapter 尚未验证；本次不声称 Product Use、`PUBLIC_E2E`、
`HUMAN_ACCEPTANCE`、scientific validity 或 `PROMOTE/B2`。CR-008 不缩减最终产品
scope；每项实现仍须在现有 source/Evidence、人工决策、release 和 promotion 边界
内单独验证，未知权利保持阻断。

### CR-009 — Agent-first Product E2E 验收

CR-009 新增 Agent-first 产品验收要求，不缩减或替换 FR-001..FR-026、CR-008
或最终产品范围。它要求在干净的 `review-writer-product-package` checkout 中，
以普通用户可提供的最小输入运行真实 Agent：`topic`、显式 `project_root` 和获
授权的 `pdf_folder`。测试 Agent 不得运行 CLI、`curl`、`pytest`、内部 generator，
也不得直接读写 `VersionContext` 或项目内部 JSON；它只能通过公开 Agent/Skill
入口和 Dashboard 完成以下产品链：

```text
source mapping
  -> parse-quality review
  -> Paper Evidence
  -> Comparison Protocol
  -> Synthesis
  -> Section Contract
  -> v1/v2 manuscript
  -> figure decision
  -> Markdown/DOCX release
```

每一个需要研究者判断的人工闸门都必须向 Agent 返回
`HUMAN_ACTION_REQUIRED`，Agent 不得替研究者批准。研究者完成决定后，Agent 必须
使用同一个显式 `project_root` 冷恢复并消费当前 `VersionContext`。测试必须证明
Agent 没有绕过 public entry、硬编码 endpoint、手写内部状态、制造无来源 claim
或无来源图件；任何一项无法证明时保持 `HOLD`。

每次 Agent E2E 必须保留可审计 receipt，至少包含：model/provider/version 与初始
prompt、tool/skill 调用序列、每次 `HUMAN_ACTION_REQUIRED` 与研究者操作、显式
project root、每个输入 PDF 的 SHA-256，以及最终 `VersionContext`、Markdown、DOCX
与 release snapshot 的身份/摘要。结果只能使用 `AGENT_E2E_PASS`、`AGENT_E2E_HOLD`
或 `AGENT_E2E_FAIL`；Engineering PASS、focused API test 或静态路径不得冒充 Agent
验收。

验证层级按以下顺序单独报告：每个微小提交的 focused unit/API test、每个垂直切片的
一次真实 Agent smoke、收尾阶段的真实 N=3 Agent-first 全流程，以及最终发布前
N=1/3/10/20 分层 Agent E2E；另测 cold-process resume、stale release、错误人工
决策和重复操作。通过条件至少是：Agent 能发现入口、调用公开能力、在人工闸门停止、
研究者决策后恢复、持续使用 canonical authority、生成同版本 Markdown/DOCX，并在
项目重新启动后恢复。

本合同切片只登记要求，当前状态为 `PLANNED/HOLD`：尚无完整 Agent E2E harness、
真实 N=3 receipt、cold-resume receipt 或 `HUMAN_ACCEPTANCE` 证据，因此不宣称
`AGENT_E2E_PASS`，也不把现有 Engineering/focused 测试升级为产品验收。

## 交付与 N 边界

最终合同不固定 `N` 为 `1–3` 或 `20–40`；N 是授权 source set 的实际计数。当前
实现或旧用户说明中出现的 `1–3` 只能标为待 FR-005 验证的临时 `HOLD`，不能作为
最终产品上限。FR-005 需要覆盖 N=1/3/10/20 的可用性与性能边界，并单独报告结果。

本次公开合同提交不宣称任何未运行测试、Product Use、`PUBLIC_E2E`、
`HUMAN_ACCEPTANCE`、scientific validity 或 `PROMOTE/B2` 已通过。
