# Review Writer 产品包

> **非正式预览 / unofficial preview**：这是工程发布预览包，只证明已打包的本地运行时可以接受工程 smoke 检查；它不宣称科学有效性、`PUBLIC_E2E`、`HUMAN_ACCEPTANCE` 或任何发布批准已经成立。

Review Writer 是一个本地优先的化学综述工作台。你在 Codex 中说明主题、一个明确的项目目录和获授权的本地 PDF 文件夹；Agent 使用本包的本地工具，遇到需要研究者判断的地方暂停并返回 Dashboard。PDF、项目正文和版本历史都保留在你的电脑上。

## 完整流程

### 1. 获取产品包

GitHub 仓库地址：<https://github.com/kenqia/review-writer-product-package>

优先使用 GitHub Desktop：

1. 打开上面的仓库页面，点击 **Code -> Open with GitHub Desktop**。
2. 在 GitHub Desktop 选择本机保存位置并点击 **Clone**。
3. Clone 后得到的 `review-writer-product-package` 就是仓库根目录。后续在 Codex Desktop 中直接打开这个目录，不要假定其中还有第二层同名目录。

没有 GitHub Desktop 时，在仓库网页点击 **Code -> Download ZIP**，解压后直接打开解压得到的仓库根目录。其名称可能带有 `-main` 后缀；只要该目录内有 `README.md`、`requirements.txt`、`review_writer/` 和 `view/`，它就是应打开的 package 根目录。

可选的命令行方式：

```bash
git clone https://github.com/kenqia/review-writer-product-package.git
```

Download ZIP 不带 Git 元数据；GitHub Desktop 或 `git clone` 会在你的本机 clone 中保留正常的 `.git`。无论哪种方式，都不要把 PDF、项目数据、token、cookie 或 `.env` 放进这个 package 根目录。

### 2. 在 Codex Desktop 打开 package 根目录

启动 Codex Desktop，使用 **Add Project** 或 **Open Folder**，选择上一步得到的 package 根目录。例如：

```text
C:\Users\researcher\Documents\review-writer-product-package
```

或：

```text
/home/researcher/review-writer-product-package
```

不要只打开 `review_writer/` 子目录，也不要打开未来综述的 project root。打开 package 根目录后，新建一个 Codex task；在输入框键入 `$review-orchestrator`，确认候选项或上下文显示 project-local **review-orchestrator**。这只是确认 Skill 已被发现，不需要运行任何内部命令。

### 3. 一次性安装 Python 与依赖

需要 Python 3.11 或更高版本。以下命令只用于首次安装或更换电脑，不是日常综述工作流。

**Windows PowerShell**

```powershell
cd "$HOME\Documents\review-writer-product-package"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果你的电脑没有 `py` 命令，请安装 Python 3.11+ 后把第一条创建环境的命令改为 `python -m venv .venv`。本地 PDF fallback 还需要 Poppler 的 `pdftotext`；安装 Poppler for Windows 并把其 `Library/bin` 加入 `PATH`，然后新开 PowerShell 运行 `pdftotext -v` 确认。

**WSL / Linux**

```bash
cd /home/researcher/review-writer-product-package
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
sudo apt-get install poppler-utils
```

一次性本地验证：

```bash
python -c "import jsonschema, PIL, docx, review_writer; from view.serve_review_dashboard import dashboard_assets; from pathlib import Path; print('imports: OK; dashboard assets:', len(dashboard_assets(Path('view'))))"
```

看到 `imports: OK` 即可。`latex2word` 是可选的数学排版增强；缺失时 DOCX helper 会保留基本导出并对数学排版降级，不会自动联网或索取 token。

### 4. 准备一次综述的两个独立目录

不要在 package 根目录内放综述数据。准备：

1. 一个**空** project root，例如 `/home/researcher/review-projects/nickel-coupling-review`。
2. 一个 authorized PDF folder，例如 `/home/researcher/authorized-pdfs/nickel-coupling`，其中只放本次明确授权读取的 1 至 3 个 PDF。

Windows 用户可使用同样含义的路径，例如 `C:\Users\researcher\Documents\review-projects\nickel-coupling-review` 和 `C:\Users\researcher\Documents\authorized-pdfs\nickel-coupling`。新建 project root 必须为空；恢复既有综述时必须使用原来的同一个 root。

### 5. 在 Codex 中粘贴自然语言请求

在已经打开 package 根目录的 Codex task 中粘贴以下文字，并替换主题和两条路径：

```text
请使用 Review Writer 为《可见光驱动的镍催化偶联》创建综述项目。
项目目录为 /home/researcher/review-projects/nickel-coupling-review（这是一个空目录）。
允许读取 PDF 文件夹 /home/researcher/authorized-pdfs/nickel-coupling。
需要我判断 MAIN/SI 身份或 Evidence 时，请返回 Dashboard 并暂停。
```

日常产品使用只需这类自然语言请求。不要要求用户运行 CLI、`curl`、`pytest`、`generator-start`、`generator-continue` 或任何内部 helper；这些是 Agent 的内部执行细节。

### 6. 打开 Dashboard 并完成研究者操作

Agent 到达人工 gate 时会返回一个本机 Dashboard 链接。直接点击 Agent 返回的链接，然后按界面完成：

1. 在来源与证据区域核对 PDF、MAIN/SI 身份、解析质量与 Evidence decision。
2. 在正文区域编辑并批准内容，保留 `USER_EDITED` 或 `RESEARCHER_AUTHORED` 标记与理由。
3. 在图表区域检查来源署名、许可上下文和正文绑定；不完整时保持 GAP。
4. 在发布区域查看质量状态，并导出同一版本的 Markdown 和 DOCX。
5. 正文改变后，旧 release 会标记为 stale；通过 Dashboard 的 regenerate 得到新版本，不能把旧文件当作最新发布物。
6. 在 History 查看、compare、branch 或 undo；仅查看历史不应移动 current。

Dashboard 是人工工作台，不是第二事实源，也不会替你批准科学结论。项目目录中的 `.review-writer/version_context` 是 sources、Evidence、正文、图件、release、current 和版本的唯一 durable authority。

### 7. 关闭后继续同一综述

关闭 Dashboard 或退出 Codex 后，不要复制项目数据回 package 根目录。下次在 Codex Desktop 打开同一个 package 根目录，再说：

```text
从 /home/researcher/review-projects/nickel-coupling-review 继续。
```

Agent 会读取这个同一 project root 的 current/version context，并在需要时返回新的本机 Dashboard 链接。继续流程仍不需要用户手工运行内部 CLI。

## 质量边界

本包的 import smoke、Dashboard 静态资产检查和运行时测试只说明 Engineering 层可以启动。它们不等同于 `Independent Quality`、`Product Use`、`PUBLIC_E2E`、`HUMAN_ACCEPTANCE`、科学有效性或 `PROMOTE/B2`。来源身份、化学字段、图表归属和最终发布仍必须由研究者在 Dashboard 中逐项核对；不确定时保持 `HUMAN_ACTION_REQUIRED`、`GAP` 或 `HOLD`。
