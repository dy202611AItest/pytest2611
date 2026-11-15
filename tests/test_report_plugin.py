"""报告插件的单元测试：只测纯逻辑，不真正发网络请求。

注意：插件用模块级 `_state` 保存一次会话的数据，测试里统一替换成新的
`_State`，避免污染当前 pytest 会话最终生成的报告。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins import report_plugin as rp


class FakeConfig:
    """够用的 pytest.Config 替身，只实现插件用到的方法。"""

    def __init__(self, rootpath: Path | str = ".", **options: object) -> None:
        self.rootpath = Path(rootpath)
        self._options = options

    def getoption(self, name: str, default: object = None) -> object:
        return self._options.get(name, default)


def _case(
    nodeid: str,
    outcome: str,
    duration: float = 0.001,
    message: str = "",
) -> dict:
    file, _, name = nodeid.partition("::")
    return {
        "nodeid": nodeid,
        "name": name or nodeid,
        "file": file,
        "line": 1,
        "markers": [],
        "outcome": outcome,
        "duration": duration,
        "message": message,
    }


def _summary(cases: list[dict], collect_errors: list[str] | None = None) -> dict:
    outcomes = [c["outcome"] for c in cases]
    passed = outcomes.count("passed")
    return {
        "title": "单测报告",
        "started_at": "2026-01-01 00:00:00",
        "finished_at": "2026-01-01 00:00:01",
        "duration": 1.0,
        "exitstatus": 1 if {"failed", "error"} & set(outcomes) else 0,
        "rootdir": "/tmp/demo",
        "environment": {
            "python": "3.11.0",
            "platform": "Linux",
            "pytest": "8.0.0",
            "host": "ci",
            "user": "runner",
        },
        "stats": {
            "total": len(cases),
            "executed": len(cases),
            "passed": passed,
            "failed": outcomes.count("failed"),
            "errors": outcomes.count("error"),
            "skipped": outcomes.count("skipped"),
            "xfailed": outcomes.count("xfailed"),
            "xpassed": outcomes.count("xpassed"),
            "pass_rate": round(passed / len(cases) * 100, 1) if cases else 0.0,
        },
        "collect_errors": collect_errors or [],
        "cases": cases,
    }


@pytest.fixture
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> rp._State:
    state = rp._State()
    monkeypatch.setattr(rp, "_state", state)
    return state


# ---------------------------------------------------------------- 统计
def test_stats_counts_each_outcome() -> None:
    state = rp._State()
    for nodeid, outcome in [
        ("a", "passed"),
        ("b", "passed"),
        ("c", "failed"),
        ("d", "error"),
        ("e", "skipped"),
    ]:
        state.ensure_case(nodeid).outcome = outcome
    state.total_collected = 6

    stats = state.stats()

    assert stats["total"] == 6  # 用例总数以收集数为准
    assert stats["executed"] == 5
    assert stats["passed"] == 2
    assert stats["failed"] == 1
    assert stats["errors"] == 1
    assert stats["pass_rate"] == 40.0


# ---------------------------------------------------------------- 文本工具
def test_clip_strips_ansi_and_keeps_tail() -> None:
    text = "\x1b[31m" + "x" * 100 + "尾部关键信息" + "\x1b[0m"

    clipped = rp._clip(text, 10)

    assert "\x1b" not in clipped
    assert clipped.endswith("尾部关键信息")


def test_clip_bytes_never_splits_chinese() -> None:
    clipped = rp._clip_bytes("中文失败详情" * 2000, 4000)

    assert len(clipped.encode("utf-8")) <= 4000
    assert clipped.endswith("完整内容见本地报告")
    assert clipped.encode("utf-8").decode("utf-8")  # 没有半个字符


# ---------------------------------------------------------------- 报告内容
def test_markdown_has_sections_and_stats() -> None:
    summary = _summary(
        [
            _case("tests/test_demo.py::test_ok", "passed"),
            _case("tests/test_demo.py::test_bad", "failed", message="assert 1 == 2"),
        ],
        collect_errors=["ImportError: no module"],
    )

    md = rp._build_markdown(summary)

    assert "## 一、结果概览" in md
    assert "## 二、失败用例" in md
    assert "## 三、收集错误" in md
    assert "## 四、全部用例" in md
    assert "| 通过率 | 50.0% |" in md
    assert "tests/test_demo.py::test_bad" in md


def test_html_escapes_case_content() -> None:
    summary = _summary(
        [
            _case(
                "tests/test_xss.py::test_escape",
                "failed",
                message="<script>alert('boom')</script>",
            )
        ]
    )

    html = rp._build_html(summary)

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert 'class="tag failed"' in html
    assert "通过率" in html


# ---------------------------------------------------------------- 落盘
def test_write_reports_creates_three_formats(tmp_path: Path, fresh_state: rp._State) -> None:
    config = FakeConfig(rootpath=tmp_path, report_dir="reports")

    rp._write_reports(config, _summary([_case("tests/a.py::test_a", "passed")]))

    out = tmp_path / "reports"
    assert (out / "test-report.json").is_file()
    assert (out / "test-report.md").is_file()
    assert (out / "test-report.html").is_file()
    assert (
        json.loads((out / "test-report.json").read_text(encoding="utf-8"))["stats"]["passed"] == 1
    )
    assert fresh_state.report_html == out / "test-report.html"


# ---------------------------------------------------------------- 企业微信
def test_resolve_webhook_cli_wins(monkeypatch: pytest.MonkeyPatch, fresh_state: rp._State) -> None:
    monkeypatch.setenv("WECOM_WEBHOOK", "https://env.example/hook")

    assert rp._resolve_webhook(FakeConfig(report_webhook="https://cli.example/hook")) == (
        "https://cli.example/hook"
    )
    assert rp._resolve_webhook(FakeConfig(report_webhook=None)) == "https://env.example/hook"


def test_send_policy_never_skips(fresh_state: rp._State) -> None:
    summary = _summary([_case("tests/a.py::test_a", "failed")])

    rp._send_by_policy(FakeConfig(report_send="never"), summary)

    assert "never" in fresh_state.send_note


def test_send_policy_on_failure_skips_green_run(fresh_state: rp._State) -> None:
    summary = _summary([_case("tests/a.py::test_a", "passed")])

    rp._send_by_policy(FakeConfig(report_send="on-failure"), summary)

    assert "跳过" in fresh_state.send_note


def test_wecom_markdown_stays_within_byte_limit() -> None:
    cases = [
        _case(f"tests/test_many.py::test_{i}", "failed", message="boom\n" * 200) for i in range(30)
    ]

    content = rp._build_wecom_markdown(_summary(cases))

    assert len(content.encode("utf-8")) <= rp.WECOM_CONTENT_LIMIT


# ---------------------------------------------------------------- GitHub Actions
def test_github_integration_is_noop_outside_actions(
    monkeypatch: pytest.MonkeyPatch, fresh_state: rp._State
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    rp._publish_to_github(_summary([_case("tests/a.py::test_a", "passed")]))

    assert fresh_state.github_note == ""


def test_github_integration_writes_summary_outputs_and_annotations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_state: rp._State,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary_path = tmp_path / "summary.md"
    output_path = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    summary = _summary(
        [
            _case("tests/test_demo.py::test_ok", "passed"),
            _case("tests/test_demo.py::test_bad", "failed", message="assert 1 == 2"),
        ]
    )

    rp._publish_to_github(summary)

    outputs = output_path.read_text(encoding="utf-8")
    assert "passed=1" in outputs
    assert "failed=1" in outputs
    assert "## 一、结果概览" in summary_path.read_text(encoding="utf-8")
    assert "::error file=tests/test_demo.py,line=1" in capsys.readouterr().out
    assert "Summary" in fresh_state.github_note


def test_escape_cmd_handles_workflow_command_chars() -> None:
    assert rp._escape_cmd("50%\r\n") == "50%25%0D%0A"
    assert rp._escape_cmd("a:b,c", prop=True) == "a%3Ab%2Cc"
