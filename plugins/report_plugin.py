"""pytest 自定义报告插件。

一句话：用 hook 抓一次测试运行的全部数据，落地成 JSON/Markdown/HTML 报告，
再按策略把摘要推送到企业微信群机器人；在 GitHub Actions 里额外同步
Job Summary、失败注解和 step outputs，方便 CI/CD 直接消费。

hook 与职责的对应关系：

    pytest_sessionstart        会话开始 → 记录开始时间、创建报告目录
    pytest_collection_modifyitems 收集完成 → 记录文件、行号、marker
    pytest_collection_finish   收集完成 → 记录用例总数（其实是 session.items 的长度）
    pytest_runtest_logreport   每条用例的 setup/call/teardown → 记录结果、耗时、失败原因
    pytest_sessionfinish       会话结束 → 算总时长、写报告、同步 GitHub、发机器人
    pytest_terminal_summary    在终端末尾打印报告路径，方便直接点开

设计原则（很重要）：
报告和通知都属于"附加功能"，插件本身**永远不能把测试跑挂**。
所以落盘、发送全部用 try/except 包住，失败只打印警告。
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path

import pytest

# 企业微信机器人 markdown 内容上限是 4096 字节（不是字符），发太大会被拒
WECOM_CONTENT_LIMIT = 4000
WEBHOOK_ENV_NAMES = ("WECOM_WEBHOOK", "WECHAT_WEBHOOK")
# GitHub 单次运行最多展示 10 条同级别注解，多了会被截断
GITHUB_MAX_ANNOTATIONS = 10

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class CaseResult:
    """一条用例的最终结果。"""

    nodeid: str
    name: str = ""
    file: str = ""
    line: int = 0
    markers: list[str] = field(default_factory=list)
    outcome: str = "unknown"  # passed/failed/error/skipped/xfailed/xpassed
    duration: float = 0.0
    message: str = ""

    @property
    def icon(self) -> str:
        return {
            "passed": "✅",
            "failed": "❌",
            "error": "💥",
            "skipped": "⏭️",
            "xfailed": "🟡",
            "xpassed": "🟠",
        }.get(self.outcome, "❔")


class _State:
    """插件全局状态。pytest 是单进程的，用模块级单例足够。"""

    def __init__(self) -> None:
        self.started_perf = 0.0
        self.started_at = ""
        self.finished_at = ""
        self.wall_duration = 0.0
        self.total_collected = 0
        self.cases: dict[str, CaseResult] = {}
        self.meta: dict[str, dict] = {}
        self.collect_errors: list[str] = []
        self.exitstatus = 0
        self.report_md: Path | None = None
        self.report_json: Path | None = None
        self.report_html: Path | None = None
        self.out_dir: Path | None = None
        self.send_note = "未执行"
        self.github_note = ""

    # ---------------------------------------------------------------- 工具
    def ensure_case(self, nodeid: str) -> CaseResult:
        case = self.cases.get(nodeid)
        if case is None:
            meta = self.meta.get(nodeid, {})
            case = CaseResult(
                nodeid=nodeid,
                name=meta.get("name", nodeid.split("::")[-1]),
                file=meta.get("file", ""),
                line=meta.get("line", 0),
                markers=meta.get("markers", []),
            )
            self.cases[nodeid] = case
        return case

    def stats(self) -> dict:
        outcomes = [c.outcome for c in self.cases.values()]
        passed = outcomes.count("passed")
        failed = outcomes.count("failed")
        errors = outcomes.count("error")
        skipped = outcomes.count("skipped")
        xfailed = outcomes.count("xfailed")
        xpassed = outcomes.count("xpassed")
        executed = passed + failed + errors + skipped + xfailed + xpassed
        return {
            "total": self.total_collected or executed,
            "executed": executed,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "xfailed": xfailed,
            "xpassed": xpassed,
            "pass_rate": round(passed / executed * 100, 1) if executed else 0.0,
        }


_state = _State()


# --------------------------------------------------------------------------
# 1) 命令行参数
# --------------------------------------------------------------------------
def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("custom-report", "自定义测试报告")
    group.addoption(
        "--report-dir",
        default="reports",
        help="报告输出目录，相对项目根目录（默认 reports）",
    )
    group.addoption(
        "--report-send",
        choices=["always", "on-failure", "never"],
        default="on-failure",
        help="发送策略：always 总是发 / on-failure 仅失败时发（默认）/ never 不发",
    )
    group.addoption(
        "--report-webhook",
        default=None,
        help="企业微信机器人 webhook；也可用环境变量 WECOM_WEBHOOK",
    )
    group.addoption(
        "--report-title",
        default="接口自动化测试报告",
        help="报告标题",
    )


# --------------------------------------------------------------------------
# 2) 收集阶段
# --------------------------------------------------------------------------
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """收集完用例后，把文件/行号/标记先存好，后面报告直接用。"""
    for item in items:
        path = item.path
        with contextlib.suppress(ValueError):
            path = item.path.relative_to(config.rootpath)
        _state.meta[item.nodeid] = {
            "name": item.name,
            "file": path.as_posix(),
            "line": item.location[1] + 1,
            "markers": sorted({m.name for m in item.iter_markers()} - {"parametrize"}),
        }


def pytest_collection_finish(session: pytest.Session) -> None:
    """这里拿到的是"收集到的用例总数"（含参数化展开后的数量）。"""
    _state.total_collected = len(session.items)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    """收集阶段自身报错（比如 import 失败）也要记录，否则报告会少一块。"""
    if report.failed:
        _state.collect_errors.append(_clip(str(report.longrepr), 1500))


# --------------------------------------------------------------------------
# 3) 会话开始 / 结束
# --------------------------------------------------------------------------
def pytest_sessionstart(session: pytest.Session) -> None:
    _state.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _state.started_perf = time.perf_counter()
    # 提前建目录：pytest-cov / junitxml 可能在 sessionfinish 里先写文件，
    # 不能等本插件的 sessionfinish 才创建 reports/。
    _state.out_dir = Path(session.config.rootpath) / str(session.config.getoption("report_dir"))
    try:
        _state.out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"\n[custom-report] 报告目录创建失败：{exc}")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _state.wall_duration = time.perf_counter() - _state.started_perf
    _state.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _state.exitstatus = int(exitstatus)

    summary = _build_summary(session)
    _write_reports(session.config, summary)
    _publish_to_github(summary)

    # --collect-only 时只收集不执行，不应该推送
    if session.config.option.collectonly:
        _state.send_note = "collect-only 模式，跳过发送"
        return
    _send_by_policy(session.config, summary)


# --------------------------------------------------------------------------
# 4) 每条用例执行结束
# --------------------------------------------------------------------------
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """一条用例会触发 3 次：setup、call、teardown，按阶段合并结果。"""
    case = _state.ensure_case(report.nodeid)
    case.duration += report.duration

    if report.when == "setup":
        if report.failed:
            case.outcome = "error"
            case.message = _clip(str(report.longrepr), 3000)
        elif report.skipped:
            case.outcome = "skipped"
            case.message = _clip(str(report.longrepr), 1000)

    elif report.when == "call":
        if report.passed:
            case.outcome = "passed"
        elif report.failed:
            case.outcome = "failed"
            case.message = _clip(str(report.longrepr), 3000)
        elif report.skipped:
            if hasattr(report, "wasxfail"):
                case.outcome = "xfailed"
            else:
                case.outcome = "skipped"
            case.message = _clip(str(report.longrepr), 1000)

    elif report.when == "teardown" and report.failed:
        # 清理阶段失败也要记录；但如果用例本身就是 failed，别把结果改成 error
        detail = _clip(str(report.longrepr), 3000)
        if case.outcome in ("failed", "error"):
            case.message = f"{case.message}\n\nteardown 阶段也失败：\n{detail}".strip()
        else:
            case.outcome = "error"
            case.message = detail


# --------------------------------------------------------------------------
# 5) 终端末尾提示
# --------------------------------------------------------------------------
def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    if not _state.report_md:
        return
    terminalreporter.write_sep("-", "自定义测试报告")
    terminalreporter.write_line(f"Markdown : {_state.report_md}")
    terminalreporter.write_line(f"JSON     : {_state.report_json}")
    if _state.report_html:
        terminalreporter.write_line(f"HTML     : {_state.report_html}")
    if _state.github_note:
        terminalreporter.write_line(f"GitHub   : {_state.github_note}")
    terminalreporter.write_line(f"企业微信 : {_state.send_note}")


# --------------------------------------------------------------------------
# 报告构建
# --------------------------------------------------------------------------
def _build_summary(session: pytest.Session) -> dict:
    stats = _state.stats()
    cases = sorted(_state.cases.values(), key=lambda c: c.nodeid)
    return {
        "title": session.config.getoption("report_title"),
        "started_at": _state.started_at,
        "finished_at": _state.finished_at,
        "duration": round(_state.wall_duration, 3),
        "exitstatus": _state.exitstatus,
        "rootdir": str(session.config.rootpath),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(terse=True),
            "pytest": pytest.__version__,
            "host": platform.node(),
            "user": os.environ.get("USERNAME") or os.environ.get("USER", "unknown"),
        },
        "stats": stats,
        "collect_errors": _state.collect_errors,
        "cases": [
            {
                "nodeid": c.nodeid,
                "name": c.name,
                "file": c.file,
                "line": c.line,
                "markers": c.markers,
                "outcome": c.outcome,
                "duration": round(c.duration, 4),
                "message": c.message,
            }
            for c in cases
        ],
    }


def _write_reports(config: pytest.Config, summary: dict) -> None:
    out_dir = _state.out_dir or (Path(config.rootpath) / str(config.getoption("report_dir")))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # 磁盘满了/没权限都不该让测试失败
        print(f"\n[custom-report] 报告目录创建失败：{exc}")
        return

    # 三种格式互不影响：HTML 写不出来也不该拖累 JSON / Markdown
    artifacts = (
        ("test-report.json", json.dumps(summary, ensure_ascii=False, indent=2), "report_json"),
        ("test-report.md", _build_markdown(summary), "report_md"),
        ("test-report.html", _build_html(summary), "report_html"),
    )
    for filename, content, attr in artifacts:
        try:
            path = out_dir / filename
            path.write_text(content, encoding="utf-8")
            setattr(_state, attr, path)
        except OSError as exc:
            print(f"\n[custom-report] {filename} 写入失败：{exc}")


def _build_markdown(summary: dict) -> str:
    stats = summary["stats"]
    env = summary["environment"]

    header = [
        f"# {summary['title']}",
        "",
        f"- 开始时间：{summary['started_at']}",
        f"- 结束时间：{summary['finished_at']}",
        f"- 总耗时：**{summary['duration']:.2f}s**",
        f"- 运行环境：{env['platform']} / Python {env['python']} / pytest {env['pytest']}",
        f"- 执行人：{env['user']}@{env['host']}",
        f"- 退出码：{summary['exitstatus']}",
        "",
    ]

    overview = [
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 用例总数 | {stats['total']} |",
        f"| 通过 | {stats['passed']} |",
        f"| 失败 | {stats['failed']} |",
        f"| 错误 | {stats['errors']} |",
        f"| 跳过 | {stats['skipped']} |",
        f"| 预期失败 (xfail) | {stats['xfailed']} |",
        f"| 意外通过 (xpass) | {stats['xpassed']} |",
        f"| 通过率 | {stats['pass_rate']:.1f}% |",
        "",
    ]

    # 章节按实际内容动态编号，避免出现"一、二、四"这种跳号
    sections: list[tuple[str, list[str]]] = [("结果概览", overview)]

    failures = [c for c in summary["cases"] if c["outcome"] in ("failed", "error")]
    if failures:
        body: list[str] = []
        for case in failures:
            body += [
                f"### `{case['nodeid']}`",
                "",
                f"- 位置：`{case['file']}:{case['line']}`",
                f"- 耗时：{case['duration']:.3f}s",
                "",
                "```text",
                case["message"] or "（无输出）",
                "```",
                "",
            ]
        sections.append(("失败用例", body))

    if summary["collect_errors"]:
        body = []
        for err in summary["collect_errors"]:
            body += ["```text", err, "```", ""]
        sections.append(("收集错误", body))

    body = ["| 状态 | 用例 | 耗时 |", "| --- | --- | --- |"]
    for case in summary["cases"]:
        body.append(f"| {case['outcome']} | `{case['nodeid']}` | {case['duration']:.3f}s |")
    sections.append(("全部用例", body))

    numerals = "一二三四五六七八九十"
    out = header
    for index, (title, content) in enumerate(sections):
        out.append(f"## {numerals[index]}、{title}")
        out.append("")
        out.extend(content)
    out.append("")
    return "\n".join(out)


_HTML_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f6f8fa; --card: #ffffff; --text: #1f2328; --muted: #656d76;
  --line: #d0d7de; --ok: #1a7f37; --bad: #cf222e; --warn: #9a6700;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --card: #161b22; --text: #e6edf3; --muted: #8d96a0;
    --line: #30363d; --ok: #3fb950; --bad: #f85149; --warn: #d29922;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 40px 24px; background: var(--bg); color: var(--text);
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 8px; }
h2 {
  font-size: 19px; margin: 36px 0 12px; padding-bottom: 6px;
  border-bottom: 1px solid var(--line);
}
.meta { color: var(--muted); font-size: 13px; margin: 0; }
.meta span { margin-right: 16px; white-space: nowrap; }
.cards {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 12px; margin-top: 24px;
}
.card {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 14px 16px;
}
.card .label {
  color: var(--muted); font-size: 12px; letter-spacing: .04em;
  text-transform: uppercase;
}
.card .value { font-size: 24px; font-weight: 600; margin-top: 2px; }
.card.ok .value { color: var(--ok); }
.card.bad .value { color: var(--bad); }
.card.warn .value { color: var(--warn); }
table {
  width: 100%; border-collapse: collapse; background: var(--card);
  border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
}
th, td {
  text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line);
  font-size: 13px; vertical-align: top;
}
th { background: rgba(127,127,127,.08); font-weight: 600; }
tr:last-child td { border-bottom: none; }
code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12.5px; }
.fail {
  border: 1px solid var(--line); border-left: 3px solid var(--bad);
  border-radius: 8px; background: var(--card); padding: 12px 16px;
  margin-bottom: 12px;
}
.fail h3 { margin: 0 0 6px; font-size: 14px; font-weight: 600; }
pre {
  margin: 8px 0 0; padding: 12px; overflow: auto; max-height: 340px;
  background: rgba(127,127,127,.1); border-radius: 6px; font-size: 12.5px;
  white-space: pre-wrap; word-break: break-word;
}
.tag {
  display: inline-block; padding: 0 8px; border-radius: 999px;
  font-size: 12px; font-weight: 600; border: 1px solid currentColor;
}
.tag.passed { color: var(--ok); }
.tag.failed, .tag.error { color: var(--bad); }
.tag.skipped, .tag.xfailed, .tag.xpassed { color: var(--warn); }
footer { margin-top: 40px; color: var(--muted); font-size: 12px; }
"""


def _build_html(summary: dict) -> str:
    """生成单文件 HTML 报告：不依赖 CDN / Jinja2，直接丢给浏览器或 GitHub Pages。"""
    stats = summary["stats"]
    env = summary["environment"]
    healthy = not (stats["failed"] or stats["errors"] or summary["collect_errors"])

    cards = [
        ("用例总数", stats["total"], ""),
        ("通过", stats["passed"], "ok"),
        ("失败", stats["failed"], "bad" if stats["failed"] else ""),
        ("错误", stats["errors"], "bad" if stats["errors"] else ""),
        ("跳过", stats["skipped"], ""),
        ("通过率", f"{stats['pass_rate']:.1f}%", "ok" if healthy else "warn"),
    ]

    out = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(summary['title'])}</title>",
        f"<style>{_HTML_STYLE}</style>",
        "</head>",
        "<body>",
        '<div class="wrap">',
        f"<h1>{escape(summary['title'])}</h1>",
        '<p class="meta">'
        f"<span>开始 {escape(summary['started_at'])}</span>"
        f"<span>结束 {escape(summary['finished_at'])}</span>"
        f"<span>耗时 {summary['duration']:.2f}s</span>"
        f"<span>退出码 {summary['exitstatus']}</span>"
        "</p>",
        '<div class="cards">',
    ]
    for label, value, css in cards:
        cls = f"card {css}".strip()
        out.append(
            f'<div class="{cls}"><div class="label">{label}</div>'
            f'<div class="value">{value}</div></div>'
        )
    out.append("</div>")

    failures = [c for c in summary["cases"] if c["outcome"] in ("failed", "error")]
    if failures:
        out.append("<h2>失败用例</h2>")
        for case in failures:
            out += [
                '<div class="fail">',
                f"<h3>{escape(case['nodeid'])}</h3>",
                f'<div class="meta">{escape(case["file"])}:{case["line"]}'
                f" · {case['duration']:.3f}s</div>",
                f"<pre>{escape(case['message'] or '（无输出）')}</pre>",
                "</div>",
            ]

    if summary["collect_errors"]:
        out.append("<h2>收集错误</h2>")
        for err in summary["collect_errors"]:
            out.append(f"<pre>{escape(err)}</pre>")

    out += [
        "<h2>全部用例</h2>",
        "<table>",
        "<thead><tr><th>状态</th><th>用例</th><th>耗时</th></tr></thead>",
        "<tbody>",
    ]
    for case in summary["cases"]:
        outcome = case["outcome"]
        out.append(
            f'<tr><td><span class="tag {outcome}">{outcome}</span></td>'
            f"<td><code>{escape(case['nodeid'])}</code></td>"
            f"<td>{case['duration']:.3f}s</td></tr>"
        )
    out += ["</tbody>", "</table>"]

    out += [
        "<footer>",
        f"Python {escape(env['python'])} · pytest {escape(env['pytest'])} · "
        f"{escape(env['platform'])} · {escape(env['user'])}@{escape(env['host'])}",
        "<br>由 my-pytest-demo 自定义报告插件生成",
        "</footer>",
        "</div>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------
# GitHub Actions 集成（Summary / 失败注解 / step outputs）
# --------------------------------------------------------------------------
def _is_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def _publish_to_github(summary: dict) -> None:
    """在 GitHub Actions 中把结果暴露给流水线；任何一步失败都不影响测试。"""
    if not _is_github_actions():
        return

    notes: list[str] = []
    steps = (
        ("Job Summary", lambda: _append_step_summary(summary)),
        ("失败注解", lambda: _emit_failure_annotations(summary)),
        ("outputs", lambda: _write_step_outputs(summary)),
    )
    for name, action in steps:
        try:
            action()
            notes.append(name)
        except Exception as exc:  # noqa: BLE001 - 附加功能绝不能拖垮测试
            print(f"\n[custom-report] GitHub {name} 写入失败：{exc}")
    _state.github_note = "已写入 " + " / ".join(notes) if notes else "无可用集成点"


def _append_step_summary(summary: dict) -> None:
    """把 Markdown 报告追加到 Actions 运行的 Job Summary 页面。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as fp:
        fp.write(_build_markdown(summary))
        fp.write("\n")


def _emit_failure_annotations(summary: dict) -> None:
    """用 workflow command 在 PR 的 Files changed 页面标出失败行。"""
    failures = [c for c in summary["cases"] if c["outcome"] in ("failed", "error")]
    for case in failures[:GITHUB_MAX_ANNOTATIONS]:
        title = case["name"] or case["nodeid"]
        reason = " ".join(_clip(_strip_ansi(case["message"]), 600).split())
        message = f"{case['nodeid']}: {reason}"
        print(
            f"::error file={_escape_cmd(case['file'])},line={case['line']},"
            f"title={_escape_cmd(title, prop=True)}::{_escape_cmd(message)}"
        )
    for err in summary["collect_errors"][:3]:
        text = " ".join(err.split())
        print(f"::error title=收集错误::{_escape_cmd(_clip(text, 600))}")


def _write_step_outputs(summary: dict) -> None:
    """把统计数字写进 $GITHUB_OUTPUT，后续 step 可用 steps.<id>.outputs.failed 等。"""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    stats = summary["stats"]
    lines = [
        f"total={stats['total']}",
        f"passed={stats['passed']}",
        f"failed={stats['failed']}",
        f"errors={stats['errors']}",
        f"skipped={stats['skipped']}",
        f"pass_rate={stats['pass_rate']}",
        f"exitstatus={summary['exitstatus']}",
    ]
    if _state.report_html:
        lines.append(f"html_report={_state.report_html.as_posix()}")
    with Path(path).open("a", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")


def _escape_cmd(text: str, prop: bool = False) -> str:
    """按 GitHub workflow command 规则转义；属性值还要转义冒号和逗号。"""
    text = (text or "").replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


# --------------------------------------------------------------------------
# 企业微信推送
# --------------------------------------------------------------------------
def _resolve_webhook(config: pytest.Config) -> str:
    explicit = config.getoption("report_webhook")
    if explicit:
        return explicit.strip()
    for name in WEBHOOK_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _send_by_policy(config: pytest.Config, summary: dict) -> None:
    mode = config.getoption("report_send")
    stats = summary["stats"]
    has_problem = bool(
        stats["failed"] or stats["errors"] or summary["collect_errors"] or summary["exitstatus"]
    )

    if mode == "never":
        _state.send_note = "策略为 never，跳过"
        return
    if mode == "on-failure" and not has_problem:
        _state.send_note = "全部通过，按 on-failure 策略跳过"
        return

    webhook = _resolve_webhook(config)
    if not webhook:
        _state.send_note = "未配置 webhook（--report-webhook 或 WECOM_WEBHOOK），跳过"
        return

    content = _build_wecom_markdown(summary)
    try:
        resp = _post_json(webhook, {"msgtype": "markdown", "markdown": {"content": content}})
        if resp.get("errcode") != 0:
            raise RuntimeError(f"errcode={resp.get('errcode')} errmsg={resp.get('errmsg')}")
        _state.send_note = f"已发送（{len(content.encode('utf-8'))} 字节）"
    except Exception as exc:  # 网络问题绝不影响测试结果
        _state.send_note = f"发送失败：{exc}"


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body or "{}")


def _build_wecom_markdown(summary: dict) -> str:
    stats = summary["stats"]
    env = summary["environment"]
    healthy = stats["failed"] == 0 and stats["errors"] == 0 and not summary["collect_errors"]
    color = "info" if healthy else "warning"

    parts = [
        f"## {summary['title']}",
        f"> 开始：{summary['started_at']}",
        f"> 耗时：**{summary['duration']:.2f}s**",
        f"> 环境：Python {env['python']} / {env['platform']}",
        "",
        (
            f'**结果：<font color="{color}">通过 {stats["passed"]} / '
            f"失败 {stats['failed']} / 错误 {stats['errors']} / "
            f"跳过 {stats['skipped']}</font>**"
        ),
        f"**用例总数：{stats['total']}　通过率：{stats['pass_rate']:.1f}%**",
    ]

    failures = [c for c in summary["cases"] if c["outcome"] in ("failed", "error")]
    if failures:
        parts += ["", "**失败用例**"]
        for case in failures[:5]:
            reason = _clip(_strip_ansi(case["message"]), 200).replace("\n", " ")
            parts += [
                f'> <font color="warning">{case["nodeid"]}</font>',
                f"> {reason}",
            ]
        if len(failures) > 5:
            parts.append(f"> …另有 {len(failures) - 5} 条，详见报告")
    else:
        parts += ["", '<font color="info">全部通过 ✅</font>']

    return _clip_bytes("\n".join(parts), WECOM_CONTENT_LIMIT)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text or "")


def _clip(text: str, limit: int, keep: str = "tail") -> str:
    """截断长文本。失败信息通常有用的部分在末尾，所以默认保留尾部。"""
    text = _strip_ansi(text).strip()
    if len(text) <= limit:
        return text
    if keep == "tail":
        return "…（已截断）\n" + text[-limit:]
    return text[:limit] + "\n…（已截断）"


def _clip_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节截断，避免把中文切成半个字符。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    suffix = "\n\n> …消息过长已截断，完整内容见本地报告"
    budget = limit - len(suffix.encode("utf-8"))
    return raw[:budget].decode("utf-8", errors="ignore") + suffix
