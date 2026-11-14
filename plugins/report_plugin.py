"""pytest 自定义报告插件。

一句话：用 hook 抓一次测试运行的全部数据，落地成 JSON/Markdown 报告，
再按策略把摘要推送到企业微信群机器人。

hook 与职责的对应关系：

    pytest_sessionstart        会话开始 → 记录开始时间
    pytest_collection_finish   收集完成 → 记录用例总数（其实是 session.items 的长度）
    pytest_runtest_logreport   每条用例的 setup/call/teardown → 记录结果、耗时、失败原因
    pytest_sessionfinish       会话结束 → 算总时长、写报告、发机器人
    pytest_terminal_summary    在终端末尾打印报告路径，方便直接点开

设计原则（很重要）：
报告和通知都属于"附加功能"，插件本身**永远不能把测试跑挂**。
所以落盘、发送全部用 try/except 包住，失败只打印警告。
"""

from __future__ import annotations

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
from pathlib import Path

import pytest

# 企业微信机器人 markdown 内容上限是 4096 字节（不是字符），发太大会被拒
WECOM_CONTENT_LIMIT = 4000
WEBHOOK_ENV_NAMES = ("WECOM_WEBHOOK", "WECHAT_WEBHOOK")

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
        self.send_note = "未执行"

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
        try:
            path = item.path.relative_to(config.rootpath)
        except ValueError:
            pass
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


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _state.wall_duration = time.perf_counter() - _state.started_perf
    _state.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _state.exitstatus = int(exitstatus)

    summary = _build_summary(session)
    _write_reports(session.config, summary)

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
        # fixture 清理失败也算错误，但不能覆盖 already failed 的判断
        case.outcome = "error"
        case.message = _clip(str(report.longrepr), 3000)


# --------------------------------------------------------------------------
# 5) 终端末尾提示
# --------------------------------------------------------------------------
def pytest_terminal_summary(
    terminalreporter: "pytest.TerminalReporter",
    exitstatus: int,
    config: pytest.Config,
) -> None:
    if not _state.report_md:
        return
    terminalreporter.write_sep("-", "自定义测试报告")
    terminalreporter.write_line(f"Markdown : {_state.report_md}")
    terminalreporter.write_line(f"JSON     : {_state.report_json}")
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
    out_dir = Path(config.rootpath) / str(config.getoption("report_dir"))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        json_path = out_dir / "test-report.json"
        json_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        md_path = out_dir / "test-report.md"
        md_path.write_text(_build_markdown(summary), encoding="utf-8")

        _state.report_json = json_path
        _state.report_md = md_path
    except OSError as exc:  # 磁盘满了/没权限都不该让测试失败
        print(f"\n[custom-report] 报告写入失败：{exc}")


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
        body.append(
            f"| {case['outcome']} | `{case['nodeid']}` | {case['duration']:.3f}s |"
        )
    sections.append(("全部用例", body))

    numerals = "一二三四五六七八九十"
    out = header
    for index, (title, content) in enumerate(sections):
        out.append(f"## {numerals[index]}、{title}")
        out.append("")
        out.extend(content)
    out.append("")
    return "\n".join(out)


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
            f"**结果：<font color=\"{color}\">通过 {stats['passed']} / "
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
                f"> <font color=\"warning\">{case['nodeid']}</font>",
                f"> {reason}",
            ]
        if len(failures) > 5:
            parts.append(f"> …另有 {len(failures) - 5} 条，详见报告")
    else:
        parts += ["", "<font color=\"info\">全部通过 ✅</font>"]

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
    return "…（已截断）\n" + text[-limit:] if keep == "tail" else text[:limit] + "\n…（已截断）"


def _clip_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节截断，避免把中文切成半个字符。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    suffix = "\n\n> …消息过长已截断，完整内容见本地报告"
    budget = limit - len(suffix.encode("utf-8"))
    return raw[:budget].decode("utf-8", errors="ignore") + suffix
