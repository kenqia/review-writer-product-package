---
name: review-writer
description: 当用户要用 topic、明确 project root 和获授权 PDF folder 创建或恢复一篇可审计的化学综述时使用。
---

# Review Writer for QoderWork CN

这是一个宿主适配 Skill，不是第二套综述引擎。它把 QoderWork CN 的自然语言任务映射到
`review_writer.agent.qoderwork_adapter`，并把所有科学判断交给现有 Dashboard。

## 输入

只向用户收集三项：`topic`、`explicit project root`、`authorized PDF folder`。新项目的 root
应为空；恢复必须使用同一个 root。不要自行发现、下载或增加论文。

## 启动与恢复

新项目调用 adapter 的 `start`。它会完成授权目录预检、项目初始化、source archive 发布、
VersionContext 节点和 Dashboard 健康检查，然后通常返回 `HUMAN_ACTION_REQUIRED`。把真实
loopback Dashboard URL 展示给用户并停止。

用户说“继续”时只调用 adapter 的 `resume`，重新读取同一个 root 的 current/version，并返回
项目自己的 Dashboard URL。不要从聊天历史推断批准状态，不要写内部 JSON。

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
- 不直接读取或写入 `.review-writer/version_context`、manifest、Source Truth 或 release JSON；
- 不创建第二个 SourceRecord/Evidence、figure registry、manuscript history 或 current；
- 不把 Engineering PASS、focused test 或 QoderWork smoke 当成 PUBLIC_E2E、HUMAN_ACCEPTANCE
  或 scientific validity；
- 不读取或输出 token、API key、cookie、session 或私有日志。

## 交付边界

只有真实 Dashboard/release producer 成功消费当前 source-bound authority 后，才可报告 release
成功。否则报告 `AGENT_E2E_HOLD` 或 `AGENT_E2E_FAIL`，并指出首个可操作 blocker。
