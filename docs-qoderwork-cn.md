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
- [Windows 安装](https://docs.qoder.cn/qoderwork/installation-guide/windows-installation)：Windows 10
  64-bit、登录、更新和客户端目录授权。
- [QoderWork 扩展发布指南](https://docs.qoder.cn/qoderwork/user-guide/qoderwork-extension-release-guide-skill-plugin-connector)：
  Skill、Plugin、Connector 与 Expert Kit 的边界。

## 安装

在产品包根目录执行：

```bash
python scripts/build_qoderwork_plugin_zip.py
```

再将 `build/review-writer-cn.qoder-plugin.zip` 上传到 QoderWork CN 的
`Extensions → Expert Kits`。也可以把插件内的 `skills/review-writer/SKILL.md` 安装为独立 Skill。

## Windows 环境准备

在 PowerShell 中运行一次以下命令（`Scope Process` 只影响当前窗口，不覆盖系统策略）：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\Install-ReviewWriter.ps1
```

脚本创建或复用产品包 `.venv`、安装 Python 依赖（包括 `requests` 与 Windows 系统 CA bridge
`truststore`）、检查 `pdftotext` 并生成 Expert Kit ZIP；
失败时不删除已有环境。安装后可随时运行只读诊断：

```powershell
.\scripts\windows\Test-ReviewWriterEnvironment.ps1
```

产品包已经包含 `skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py`
作为 MinerU 官方 v4 API adapter；它复用同一个 HTTPS session 完成 API 与结果 ZIP 下载，
`pdftotext` 是它失败时的真实 fallback。Windows 上仅当组织 CA 已安装到 Windows 原生信任库时，
`truststore` 才能使用它；它不会自动信任未知或未安装的企业代理根证书。脚本不会从未知来源
下载 Poppler；请使用可信/组织批准的 Windows Poppler 构建并把 `bin` 目录加入 PATH。产品只
检查 parser 路径和 MinerU 凭据是否存在（见 [`.env.example`](.env.example)），不读取或输出
token。`.env.example` 不会被自动加载，也不应提交真实凭据。

如果需要指定额外的兼容 parser 路径，可使用 `REVIEW_WRITER_MINERU_PARSER`；通常无需设置，
因为本包已经包含官方 API adapter。

如果 MinerU 结果 CDN 的证书由组织私有 CA 签发，可在 QoderWork 当前 Windows 进程配置一个
已存在且可读的 PEM bundle：优先使用 `REVIEW_WRITER_MINERU_CA_BUNDLE`，也兼容
`REQUESTS_CA_BUNDLE`。专用变量优先；路径无效时 parser 会在写出任何结果前返回
`MINERU_CA_BUNDLE_INVALID`，TLS 校验失败时返回 `MINERU_RESULT_DOWNLOAD_TLS_HOLD` 及可操作的
CA 诊断。证书校验默认开启，产品不会静默使用 `verify=False`。

WSL/Linux 的 `~/.zshrc` 不会被 Windows QoderWork 客户端继承。若 MinerU 只在 WSL shell 中配置，
Windows 诊断会明确显示未配置 `NOTICE`，实际流程会回退到 `pdftotext`；请根据外部解析器的
官方 Windows 说明配置其凭据，或把 token 放在 `%APPDATA%\ReviewWriter\mineru_api_token`。
Review Writer 不猜测或复制任何 `MINERU_TOKEN`/API key 变量。

QoderWork CN 的登录、模型、账号和 Credits 属于 QoderWork Windows 客户端，不是 Review
Writer 的 API 配置。客户端启动后，在 `Extensions → Expert Kits` 导入
`build\review-writer-cn.qoder-plugin.zip`，然后只向 Agent 提供 topic、Windows 绝对 project
root 和 authorized PDF folder。研究者仍在 Dashboard 完成每个人工闸门；客户端的登录或
Credits 状态不能替代 `HUMAN_ACCEPTANCE`。

## 用户输入

用户只提供：topic、explicit project root、authorized PDF folder。每个人工闸门均由 Dashboard
完成，QoderWork 不替研究者批准，也不允许直接编辑项目内部 JSON。真实 QoderWork CN UI 登录、
Credits、目录授权和最终 HUMAN_ACCEPTANCE 仍需在客户端中单独验收。
