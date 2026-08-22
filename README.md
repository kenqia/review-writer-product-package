# Review Writer

本仓库是 Review Writer 的用户产品包：一个本地优先、证据可追溯、有人审闸门的化学综述工作台。
它把论文来源、解析质量、Evidence、跨研究综合、正文、图件、版本历史和 Markdown/DOCX 导出
绑定在同一个综述项目目录中。

产品的首选宿主是 QoderWork CN。用户只需用自然语言提供三项输入，宿主 Agent 负责调用本地
产品入口；研究者在 Dashboard 中阅读、判断、编辑、批准和导出。产品不会替研究者批准科学
判断，也不会把模型输出当作科学有效性证明。

公开 canonical 合同：[`docs/PRODUCT_CONTRACT.md`](docs/PRODUCT_CONTRACT.md)；FR 追踪表：
[`docs/PRODUCT_TRACEABILITY.md`](docs/PRODUCT_TRACEABILITY.md)。它们规定对外依赖边界；本
README 只负责用户入口和运行说明。当前公开验证仍应分别报告 Engineering、Product Use、
`PUBLIC_E2E`、`HUMAN_ACCEPTANCE` 和 scientific validity，不因静态文档或 focused test 自动升级。

## 你需要提供什么

每次创建综述只需要：

1. `topic`：综述主题；
2. `explicit project root`：一个明确的项目目录；新项目必须为空；
3. `authorized PDF folder`：仅包含本次明确授权读取的本地 PDF 文件夹。

恢复项目时，必须继续使用完全相同的 project root。不要把 PDF、综述数据、token、cookie 或
`.env` 放入本产品包目录。

## QoderWork CN（首选入口）

### 安装 Expert Kit

维护者或一次性安装者在产品包根目录构建插件：

```bash
python scripts/build_qoderwork_plugin_zip.py
```

生成的 `build/review-writer-cn.qoder-plugin.zip` 上传到 QoderWork CN 的：

```text
Extensions → Expert Kits
```

也可以直接安装插件内的 `skills/review-writer/SKILL.md`。插件目录是
[`qoderwork/plugins/review-writer-cn/`](qoderwork/plugins/review-writer-cn/)。

### 用户首次使用

在 QoderWork CN 中打开本产品包所在工作区，输入类似：

```text
请使用 Review Writer 创建综述。
主题：可见光驱动的镍催化偶联
项目目录：/home/researcher/review-projects/nickel-coupling-review
获授权 PDF 文件夹：/home/researcher/authorized-pdfs/nickel-coupling
```

宿主 Agent 会调用公开的 source-bound bootstrap，随后只按系统规划的 public `next_action` 消费
canonical research packet，并返回真实的本机 Dashboard URL；在需要研究者判断时停止。用户不
需要运行 CLI、curl、pytest、内部 generator 或手工编辑 JSON。Agent 不扫描仓库、不寻找内部流程，
也不生成无来源 claim 或图。

## 一次性安装依赖

需要 Python 3.11+、`jsonschema`、`Pillow`、`python-docx`、`requests` 和 `truststore`。其中
`truststore` 让 Windows 上的 MinerU HTTPS 请求优先使用系统 CA。系统还需要本地 `pdftotext`，
用于 PDF 解析 fallback；产品包现在包含官方 MinerU v4 API adapter，MinerU 是否可用由 Agent
在运行时检查并如实记录。`latex2word` 只是可选的
DOCX 数学排版增强。

WSL/Linux 示例：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows 可使用 Python Launcher 创建同等的 3.11+ 虚拟环境。依赖安装是一次性准备，不是日常
综述操作；日常操作在 QoderWork CN 和 Dashboard 中完成。

### Windows + QoderWork CN 一键准备

QoderWork CN 是 Windows 图形化宿主；先在产品包根目录打开 PowerShell，执行一次：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\Install-ReviewWriter.ps1
```

该脚本会幂等地创建或复用 `.venv`、安装 `requirements.txt`、检查 `pdftotext`，并构建
`build\review-writer-cn.qoder-plugin.zip`。它不会删除已有环境、下载未知来源的 Poppler，也
不会读取或打印 token。若 `pdftotext` 缺失，脚本会保留已完成的 Python 安装并以 `HOLD` 提醒；
请使用组织批准或可信来源的 Poppler for Windows，将包含 `pdftotext.exe` 的 `bin` 目录加入
Windows `PATH`，然后重新运行诊断：

```powershell
.\scripts\windows\Test-ReviewWriterEnvironment.ps1
```

诊断只报告 `READY/HOLD/NOTICE` 和“配置存在/缺失”，不会回显任何密钥。也可以只读查看
`.env.example`；它只记录实际支持的 `REVIEW_WRITER_MINERU_PARSER` 和可选 CA bundle 路径变量，
产品不会自动加载 `.env` 文件。

产品包自带 MinerU adapter，默认走 MinerU 官方 v4 API（`vlm` 模型），不再要求你额外
clone 一个解析器。它需要 MinerU token；token 只从当前进程的 `MINERU_API_TOKEN` 或包外
的用户文件读取：Windows 为 `%APPDATA%\ReviewWriter\mineru_api_token`，Linux/WSL 为
`~/.config/review-writer/mineru_api_token`。Windows adapter 会优先使用已安装 `truststore`
提供的系统 CA（仅使用已经安装到 Windows 信任库的根证书，不会自动信任未知代理根）；如果组织代理使用私有 CA，可在当前进程配置已存在且可读的 PEM bundle 路径：
`REVIEW_WRITER_MINERU_CA_BUNDLE`（专用设置优先）或 `REQUESTS_CA_BUNDLE`。路径无效时会在
任何 parser output 写入前 fail-closed；TLS 校验失败会给出 `MINERU_RESULT_DOWNLOAD_TLS_HOLD`
及 CA 配置诊断。产品默认始终校验证书，不提供静默 `verify=False`。诊断只显示凭据是否存在，
不显示值。凭据不可用或网络失败时，Agent 会记录真实原因并回退到 `pdftotext`，不会把
fallback 标成 MinerU。

接着从 [qoderwork.cn/download](https://qoderwork.cn/download) 安装 QoderWork CN Windows
客户端，登录后把上面生成的 ZIP 在 `Extensions → Expert Kits` 中导入。QoderWork 的账号、
模型选择和 Credits 由客户端管理；Review Writer 不需要单独的产品 API key。MinerU adapter
使用官方 API，请按 [MinerU API 文档](https://mineru.net/apiManage/docs) 申请并配置 token；
凭据不放进仓库、ZIP、`.env`
或 project root。没有可用 MinerU 时，产品会如实记录 `pdftotext` fallback 及能力缺口。

注意：Windows QoderWork 进程不会自动读取 WSL/Linux 的 `~/.zshrc`。如果你的 MinerU 配置只
存在于 WSL shell，它不会因此出现在 Windows 客户端或 PowerShell 中；请按外部解析器的官方
Windows 配置方式设置，并只在 PowerShell 中提供 parser 路径（例如当前会话的
`$env:REVIEW_WRITER_MINERU_PARSER = 'C:\\approved\\parse_review_writer_pdfs.py'`）。诊断器会
显示 MinerU `OK`、路径缺失 `HOLD` 或未配置 `NOTICE`，但永远不检查或回显 token 值。

Windows 用户输入应使用 Windows 绝对路径，例如：

```text
主题：可见光驱动的镍催化偶联
项目目录：C:\Users\your-name\review-projects\nickel-coupling-review
获授权 PDF 文件夹：C:\Users\your-name\authorized-pdfs\nickel-coupling
```

项目目录仍是唯一 durable authority；不要把 PDF、`.env` 或凭据复制到产品包目录。

## Dashboard 中的流程

产品会按公共链依次处理：

```text
source mapping
→ parse-quality review
→ Paper Evidence
→ Comparison Protocol
→ Synthesis
→ Section Contract
→ v1/v2 manuscript
→ figure decision
→ Markdown/DOCX release
```

每个人工闸门都必须由研究者在 Dashboard 完成：

- MAIN/SI 身份和来源绑定不确定时，保持 `HUMAN_ACTION_REQUIRED`；
- 解析失败时记录实际 fallback 和 provenance，不把 fallback 伪装成 MinerU；
- Evidence、Synthesis 和正文只能使用 source-bound 内容；
- 缺少来源图件、版权/授权证据或正文绑定时，图件保持 `GAP/HOLD`；
- 正文修改后，旧 release 必须标记为 stale，再通过 Dashboard regenerate；
- History、compare、branch、undo 和 cold resume 都必须沿同一个 VersionContext 进行。

Evidence 批准后，后续 Comparison Protocol、Synthesis Claim 和 Section Contract 仍是待审对象；
只有研究者分别批准后才进入正文 v1/v2。产品不会把候选对象当成批准状态。

## 继续同一项目

在 QoderWork CN 中说：

```text
从 /home/researcher/review-projects/nickel-coupling-review 继续。
```

产品会重新读取该 project root 的 current VersionContext，不会创建第二个项目状态或覆盖
`USER_EDITED` / `RESEARCHER_AUTHORED` 内容。

## 产物与权威边界

每篇综述的 project root 是唯一 durable authority：

```text
<project root>/
├── .review-writer/version_context/  # current、immutable versions、branches
├── 00_brief/                        # 主题与项目状态
├── 00_sources/                      # 来源身份、授权与获取记录
├── 01_evidence/                     # 解析、定位、Evidence 决策
├── 02_synthesis/                    # Protocol、Synthesis、Section Contract
├── 03_figures/                      # 来源图件、rights、正文绑定
├── 04_manuscript/                   # authoritative manuscript 与 lineage
└── 05_release/                      # 同版本 Markdown、DOCX、snapshot、quality
```

本产品包不保存任何一篇综述的 current，不创建第二套 SourceRecord/Evidence、figure registry、
manuscript history 或 release snapshot。

## 安全与隐私

- 产品只读取用户明确授权的 PDF folder；
- Dashboard 默认只监听 `127.0.0.1`；
- 不在仓库保存 MinerU token、API key、cookie、session 或 `.env`；
- QoderWork CN 的本地文件授权、模型供应商传输、账号和 Credits 规则以其官方设置为准；
- 研究者拒绝、撤回或保留 GAP 时，产品不会擅自放行。

## 支持边界

QoderWork CN 是正式用户入口；Codex 等其它 Agent 宿主可以复用同一 project root 和公开产品
能力，但不应创建第二套 authority。真实 QoderWork CN UI 登录、目录授权、Credits、模型版本
和 `HUMAN_ACCEPTANCE` 仍需在客户端中单独验收。

质量层级必须分开：`Engineering`、`Independent Quality`、`Product Use`、`PUBLIC_E2E`、
`HUMAN_ACCEPTANCE` 和 scientific validity 不是同一件事。任何一层未完成，都不能被 README、
focused test 或模型回复隐式升级为最终科学结论。

## 相关文档

- [产品包清单](product-package-manifest.md)
- [公开产品合同](docs/PRODUCT_CONTRACT.md)
- [FR 追踪表](docs/PRODUCT_TRACEABILITY.md)
- [第三方声明](docs/THIRD_PARTY_NOTICES.md)
- [QoderWork CN 适配说明](docs-qoderwork-cn.md)
- [QoderWork CN 官方产品说明](https://docs.qoder.cn/qoderwork/product-overview/what-is-qoderwork-cn)
- [Qoder 插件规范](https://docs.qoder.cn/qoder-plugins)
- [项目仓库](https://github.com/kenqia/review-writer-product-package)
