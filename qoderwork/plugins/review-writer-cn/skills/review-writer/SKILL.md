---
name: review-writer
description: 当用户要用 topic、明确 project root 和获授权 PDF folder 创建或恢复一篇可审计的化学综述时使用。
---

# Review Writer for QoderWork CN

这是一个宿主适配 Skill，不是第二套综述引擎。它把 QoderWork CN 的自然语言任务映射到公开
Agent/Skill 入口，并把所有科学判断交给现有 Dashboard。

## 输入

只向用户收集三项：`topic`、`explicit project root`、`authorized PDF folder`。新项目的 root
应为空；恢复必须使用同一个 root。不要自行发现、下载或增加论文。

## 启动、恢复与公共执行计划

新项目或同一项目恢复只能通过公开 Agent/Skill adapter 的 `start` / `resume` 动作。adapter
完成自己的预检和持久化后，QoderWork CN Agent 必须只消费返回的 public `next_action` 以及
canonical source-bound research packet；不得自行探索代码或猜测下一个阶段。每次 `start` 或
`resume` 返回后，先按 `next_action` 的公开状态、动作和 Dashboard URL 执行；不从聊天历史、
仓库文件或内部流程推断批准状态。

research packet 是 Agent 唯一的研究输入。它必须保持 source-bound，并携带 parse provenance、
source/study identity、page、section、exact quote、source PDF digest 与 bound parse-object
digests，以及 Evidence candidates、comparison candidates、synthesis candidates、section
 candidates、figure candidates、GAP 和 rights 状态。Agent 只能转交或展示 packet 中已有的
候选项，不能把候选项当成批准项。

`next_action.code=HUMAN_ACTION_REQUIRED`（以及 stale、缺来源、缺 locator、缺 rights 或
fail-closed 状态）是停止信号：原样展示 public Dashboard URL/说明，等待研究者在 Dashboard
处理，不能继续调用工具、生成内容或替研究者批准。

## 人工闸门

Dashboard 中必须按现有公共链处理：

1. source mapping 与 MAIN/SI 身份；
2. MinerU/解析质量及 fallback provenance；
3. Paper Evidence；
4. Comparison Protocol；
5. source-bound Synthesis；
6. Section Contract；
7. v1 → 研究者批准/编辑 → marker-preserving v2；
8. source figure decision；
9. same-version Markdown/DOCX release。

每个 `HUMAN_ACTION_REQUIRED` 都必须原样停下。拒绝、过期、缺来源、缺 locator、缺 rights、
stale current 或 release policy 失败都保持 HOLD/FAIL-CLOSED；不得替用户批准或手写补状态。

## 禁止事项

- 不要求用户运行 CLI、curl、pytest、内部 generator 或脚本；
- 只能调用公开 Agent/Skill（public Agent/public Skill）和 Dashboard；不得扫描仓库、寻找内部流程
  或读取实现代码；
- 不直接读取或写入内部 JSON、`.review-writer/version_context`、VersionContext、manifest、
  Source Truth 或 release JSON；这些由既有 producer 和 Dashboard 管理；
- 不创建第二个 SourceRecord/Evidence、figure registry、manuscript history 或 current；
- 不生成没有 source locator、quote、digest 或 rights 的 claim/figure；不补猜缺失字段，也不
  生成模型图、placeholder 或无来源图；
- Hard stop rules: do not scan the repository, do not search for internal workflow, do not run CLI,
  curl, pytest or generator, and do not emit an unsourced claim or unsourced figure.
- 不把 Engineering PASS、focused test 或 QoderWork smoke 当成 PUBLIC_E2E、HUMAN_ACCEPTANCE
  或 scientific validity；
- 不读取或输出 token、API key、cookie、session 或私有日志。

## 交付边界

只有真实 Dashboard/release producer 成功消费当前 source-bound authority 后，才可报告 release
成功。否则报告 `AGENT_E2E_HOLD` 或 `AGENT_E2E_FAIL`，并指出首个可操作 blocker。
