# my-pytest-demo

VS Code + pytest 最小可运行模板，内置「自定义测试报告 + 企业微信通知」插件。

## 环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pytest
```

## 运行测试

```powershell
python -m pytest -v
```

或在 VS Code 左侧 Testing 面板中点击运行 / 调试。

## 目录结构

```
my-pytest-demo/
├── conftest.py              # 自动加载 plugins/ 下的自定义插件
├── plugins/
│   └── report_plugin.py     # 报告插件：hook 抓数据 → 生成报告 → 发企业微信
├── src/calc.py              # 被测代码（通过 pyproject.toml 的 pythonpath 引入）
├── tests/test_calc.py       # 测试代码
├── reports/                 # 每次运行自动生成 test-report.md / .json
├── .vscode/settings.json    # 指定解释器 + 启用 pytest 发现
├── pyproject.toml           # pytest 配置
└── README.md
```

## 自定义报告插件

### 它做了什么

| pytest hook | 时机 | 插件做的事 |
| --- | --- | --- |
| `pytest_sessionstart` | 会话开始 | 记录开始时间 |
| `pytest_collection_finish` | 用例收集完成 | 记录用例总数（含参数化展开） |
| `pytest_runtest_logreport` | 每条用例 setup/call/teardown 结束 | 记录结果、耗时、失败原因 |
| `pytest_sessionfinish` | 会话结束 | 计算总时长、写报告、按策略推送企业微信 |
| `pytest_terminal_summary` | 终端输出末尾 | 打印报告路径和发送状态 |

### 输出

- `reports/test-report.md`：人看的（概览表格 + 失败详情 + 全部用例）
- `reports/test-report.json`：机器读的（便于接 CI、二次加工）

### 命令行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--report-dir` | `reports` | 报告输出目录（相对项目根目录） |
| `--report-send` | `on-failure` | `always` 总是发 / `on-failure` 仅失败时发 / `never` 不发 |
| `--report-webhook` | 空 | 企业微信机器人 webhook，优先级高于环境变量 |
| `--report-title` | `接口自动化测试报告` | 报告标题 |

### 配置企业微信机器人

1. 企业微信群 → 右上角 `...` → **消息推送** → **添加机器人** → 复制 Webhook 地址。
2. 把地址放进环境变量，**不要写进代码**：

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

### 实现要点

- **插件不能拖垮测试**：报告写入和网络发送全部 `try/except`，失败只在终端打印提示。
- **4096 字节限制**：企业微信 markdown 上限是字节数不是字符数，插件按 UTF-8 字节截断，不会切坏中文。
- **消息体只放摘要**：超过 5 条失败只列前 5 条，完整内容留在本地报告里。
- **webhook 不进仓库**：只从命令行参数或 `WECOM_WEBHOOK` 环境变量读取。

### 扩展方向

- 加 HTML 报告：在 `_write_reports` 里用 Jinja2 渲染模板。
- 加历史趋势：把每次的 `test-report.json` 按时间戳另存，再画通过率折线。
- 打包成可复用插件：把 `plugins/` 做成独立包，在 `pyproject.toml` 里声明
  `[project.entry-points.pytest11]`，这样任何项目 `pip install` 后都能用。
