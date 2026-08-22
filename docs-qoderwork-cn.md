# QoderWork CN 适配

Review Writer 产品包现在提供一个 QoderWork CN Expert Kit/Skill 适配层。QoderWork 只负责
自然语言任务、用户授权和宿主 Agent；每篇综述的 sources、Evidence、manuscript、figures、
release 和 VersionContext 仍只保留在显式 project root。

## 官方依据（2026-08-22）

- [Qoder 插件规范](https://docs.qoder.cn/qoder-plugins)：`.qoder-plugin/plugin.json`、Skill、
  `qoderwork.md` 与 ZIP 根目录结构。
- [QoderWork CN 产品说明](https://docs.qoder.cn/qoderwork/product-overview/what-is-qoderwork-cn)：
  本地文件操作、目录授权和扩展能力。
- [QoderWork CN Skills](https://docs.qoder.cn/user-guide/skills)：Skill 的 `SKILL.md` 组织方式。

## 安装

在产品包根目录执行：

```bash
python scripts/build_qoderwork_plugin_zip.py
```

再将 `build/review-writer-cn.qoder-plugin.zip` 上传到 QoderWork CN 的
`Extensions → Expert Kits`。也可以把插件内的 `skills/review-writer/SKILL.md` 安装为独立 Skill。

## 用户输入

用户只提供：topic、explicit project root、authorized PDF folder。每个人工闸门均由 Dashboard
完成，QoderWork 不替研究者批准，也不允许直接编辑项目内部 JSON。真实 QoderWork CN UI 登录、
Credits、目录授权和最终 HUMAN_ACCEPTANCE 仍需在客户端中单独验收。
