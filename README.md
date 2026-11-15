# my-pytest-demo

[![CI](https://github.com/dy202611AItest/pytest2611/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/dy202611AItest/pytest2611/actions/workflows/ci.yml)
[![CD - Publish Report](https://github.com/dy202611AItest/pytest2611/actions/workflows/cd-publish.yml/badge.svg?branch=main)](https://github.com/dy202611AItest/pytest2611/actions/workflows/cd-publish.yml)

VS Code + pytest 最小可运行模板，内置「自定义测试报告（Markdown / JSON / HTML）+ 企业微信通知」插件，
并已接入 GitHub Actions：提交即跑 lint + 多平台多版本测试，main 分支通过后自动把报告发布到 GitHub Pages。

## 特性

- **一套 pytest 配置**：`pyproject.toml` 里集中管理测试、覆盖率、ruff。
- **报告插件**：hook 抓数据 → 落盘三种格式 → 按策略推送企业微信。
- **CI**：ruff 检查 + Python 3.10/3.11/3.12 × Ubuntu/Windows 矩阵测试，覆盖率低于 90% 直接失败。
- **CD**：main 分支 CI 变绿后，自动发布测试报告与覆盖率报告到 GitHub Pages。
- **GitHub 原生集成**：Job Summary、失败行注解、step outputs 都由插件自动输出。
- **依赖自动升级**：Dependabot 每周检查 pip、每月检查 Actions。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt

python -m pytest                 # 跑全部用例，生成 reports/
```

或在 VS Code 左侧 Testing 面板点击运行 / 调试。

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `python -m pytest` | 全量测试，生成三种报告 |
| `python -m pytest -m smoke` | 只跑冒烟用例 |
| `python -m pytest -m "not slow"` | 跳过慢用例 |
| `python -m pytest --cov --cov-report=term-missing` | 带覆盖率 |
| `python -m ruff check .` | 静态检查 |
| `python -m ruff format .` | 自动格式化 |
| `python -m ruff format --check .` | 只检查格式（CI 用这个） |

## 目录结构

```
my-pytest-demo/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml               # CI：lint + 矩阵测试 + 报告产物
│   │   └── cd-publish.yml       # CD：main 通过后发布报告到 GitHub Pages
│   └── dependabot.yml           # 依赖自动升级
├── .vscode/settings.json        # 解释器、pytest 发现、src 路径
├── conftest.py                  # 自动加载 plugins/ 下的自定义插件
├── plugins/
│   └── report_plugin.py         # 报告插件：hook 抓数据 → 报告 → 企业微信 / GitHub
├── src/calc.py                  # 被测代码（通过 pyproject.toml 的 pythonpath 引入）
├── tests/
│   ├── test_calc.py             # 被测代码的用例
│   └── test_report_plugin.py    # 插件自身的单元测试
├── requirements-dev.txt         # 本地与 CI 共用依赖
├── pyproject.toml               # pytest / coverage / ruff 配置
└── README.md
```

## 自定义报告插件

### 它做了什么

| pytest hook | 时机 | 插件做的事 |
| --- | --- | --- |
| `pytest_sessionstart` | 会话开始 | 记录开始时间 |
| `pytest_collection_modifyitems` | 收集完成 | 记录文件、行号、marker |
| `pytest_collection_finish` | 收集完成 | 记录用例总数（含参数化展开） |
| `pytest_runtest_logreport` | 每条用例 setup/call/teardown 结束 | 记录结果、耗时、失败原因 |
| `pytest_sessionfinish` | 会话结束 | 算总时长、写报告、同步 GitHub、按策略推送企业微信 |
| `pytest_terminal_summary` | 终端输出末尾 | 打印报告路径、GitHub 与发送状态 |

### 输出

| 文件 | 用途 |
| --- | --- |
| `reports/test-report.md` | 人看的：概览表格 + 失败详情 + 全部用例，也用于 Job Summary |
| `reports/test-report.json` | 机器读的：CI、二次加工、历史趋势 |
| `reports/test-report.html` | 单文件网页报告，无 CDN 依赖，可直接发布到 Pages |

### 命令行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--report-dir` | `reports` | 报告输出目录（相对项目根目录） |
| `--report-send` | `on-failure` | `always` 总是发 / `on-failure` 仅失败时发 / `never` 不发 |
| `--report-webhook` | 空 | 企业微信机器人 webhook，优先级高于环境变量 |
| `--report-title` | `接口自动化测试报告` | 报告标题 |

### GitHub Actions 集成

当 `GITHUB_ACTIONS=true` 时插件会自动：

1. 把 Markdown 报告追加到 **Job Summary**（Actions 运行详情页直接可见）。
2. 对失败用例输出 `::error file=...,line=...` **注解**，PR 的 Files changed 里能定位到具体行（最多 10 条）。
3. 把统计写进 `$GITHUB_OUTPUT`，后续 step 可以直接用：

```yaml
- name: Run tests
  id: tests
  run: python -m pytest

- name: Check pass rate
  if: steps.tests.outputs.pass_rate != '100.0'
  run: echo "通过率 ${{ steps.tests.outputs.pass_rate }}%"
```

可用 outputs：`total`、`passed`、`failed`、`errors`、`skipped`、`pass_rate`、`exitstatus`、`html_report`。

以上都是「附加能力」：任何一步写失败都只打印警告，不会改变测试退出码。

### 配置企业微信机器人

1. 企业微信群 → 右上角 `...` → **消息推送** → **添加机器人** → 复制 Webhook 地址。
2. 本地放进环境变量，**不要写进代码**：

```powershell
# 当前窗口临时生效
$env:WECOM_WEBHOOK = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key'

# 永久生效（新窗口才生效）
[Environment]::SetEnvironmentVariable('WECOM_WEBHOOK', 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key', 'User')
```

3. 运行：

```powershell
python -m pytest                                   # 全部通过：不发
python -m pytest --report-send=always              # 无论如何都发
python -m pytest --report-send=never               # 只出本地报告
```

CI 里则把同样的地址存成仓库 Secret `WECOM_WEBHOOK`（见下文），插件在失败时自动推送。
来自 fork 的 PR 拿不到 Secret，插件只会提示「未配置 webhook」，不会报错。

### 实现要点

- **插件不能拖垮测试**：报告写入、GitHub 集成、网络发送全部 `try/except`，失败只在终端打印提示。
- **4096 字节限制**：企业微信 markdown 上限是字节数不是字符数，插件按 UTF-8 字节截断，不会切坏中文。
- **消息体只放摘要**：超过 5 条失败只列前 5 条，完整内容留在本地报告里。
- **webhook 不进仓库**：只从命令行参数或 `WECOM_WEBHOOK` 环境变量读取，`.gitignore` 已排除 `.env*`。
- **三种格式互不影响**：HTML 写不出来也不会影响 JSON / Markdown 落盘。

## CI/CD

### CI：`.github/workflows/ci.yml`

| Job | 内容 |
| --- | --- |
| `lint` | ruff check（告警标在 PR diff 行上）+ ruff format 检查 |
| `test` | 3 个 Python 版本 × Ubuntu / Windows 共 6 个组合，跑测试、覆盖率、生成报告并上传 artifact |
| `ci-success` | 汇总门禁，分支保护只需要求这一个检查 |

触发时机：push 到 `main`、PR、手动 `workflow_dispatch`、每天夜间回归（UTC 18:00，北京时间 02:00）。

覆盖率阈值配置在 `pyproject.toml` 的 `[tool.coverage.report] fail_under = 90`，低于阈值 CI 失败。

### CD：`.github/workflows/cd-publish.yml`

流程：`main` 上的 CI 成功 → 下载 `ubuntu-latest / 3.12` 的 `reports/` 产物 → 生成入口页 → 部署到 GitHub Pages。

发布后的地址：<https://dy202611aitest.github.io/pytest2611/>

**Pages 会自动开启**：CD 首次运行时通过 `actions/configure-pages` 的 `enablement: true` 自动把
Source 设为 **GitHub Actions**，无需手动点设置。若组织策略禁止自动开启，则手动处理：

1. 仓库 **Settings → Pages** → **Build and deployment → Source** 选择 **GitHub Actions**（不要选 Deploy from a branch）。
2. 在 **Settings → Environments** 中会看到自动创建的 `github-pages`，可按需加保护规则。
3. 手动触发：**Actions → CD - Publish Report → Run workflow**（留空 run_id 会自动取最近一次成功的 main CI）。

不想发布报告时，直接在 Actions 页面禁用该 workflow 即可，CI 不受影响。

### 配置 Secret

仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| 名称 | 必填 | 用途 |
| --- | --- | --- |
| `WECOM_WEBHOOK` | 否 | 企业微信机器人地址；不配置则只出报告不推送 |

### 分支保护建议

**Settings → Branches → Add branch protection rule**（保护 `main`）：

- Require a pull request before merging。
- Require status checks to pass → 勾选 **CI Success**。
- （可选）Require branches to be up to date before merging。

这样 PR 只有 lint 和全部 6 个测试组合都通过才能合并，合并后 CD 自动发布新报告。

### 本地复现 CI

```powershell
python -m ruff check .
python -m ruff format --check .
python -m pytest --cov --cov-report=term-missing --junitxml reports/junit.xml
```

## 扩展方向

- 加历史趋势：把每次的 `test-report.json` 按时间戳另存，再画通过率折线。
- 打包成可复用插件：把 `plugins/` 做成独立包，在 `pyproject.toml` 里声明
  `[project.entry-points.pytest11]`，任何项目 `pip install` 后都能用。
- 报告归档：把 Pages 换成对象存储 / 内部静态站点，保留更长的历史。
- 多环境测试：用 GitHub Environments + matrix 接入测试 / 预发环境地址。
