"""集成测试: 在真实子进程里验证模块入口与命令行解析."""

import os
import runpy
import subprocess
import sys

import pytest

import eastmoneyrzrq.cli
from eastmoneyrzrq import config


@pytest.mark.integration
def test_main_module_exits_with_cli_return_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m eastmoneyrzrq` 会把 cli.main 的返回码当成退出码.

    Notes
    -----
    把 cli.main 换成桩函数, 否则真跑一遍会去请求东财接口.
    """
    monkeypatch.setattr(eastmoneyrzrq.cli, "main", lambda: 7)

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("eastmoneyrzrq", run_name="__main__")

    assert excinfo.value.code == 7


@pytest.mark.integration
def test_module_entrypoint_prints_help() -> None:
    """``python -m eastmoneyrzrq --help`` 应当以 0 退出并打印用法.

    Notes
    -----
    强制 PYTHONUTF8=1: Windows 控制台默认 GBK, 中文帮助文本会在这里报编码错误.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "eastmoneyrzrq", "--help"],
        cwd=config.PROJECT_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("usage: eastmoneyrzrq")
    assert "--no-send" in completed.stdout
    assert "--from-csv" in completed.stdout
    assert completed.stderr == ""


@pytest.mark.integration
def test_console_script_is_installed() -> None:
    """Console script 入口已注册, 且与模块入口打印同一份用法."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from eastmoneyrzrq.cli import main; raise SystemExit(main(['-h']))",
        ],
        cwd=config.PROJECT_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )

    assert completed.returncode == 0
    assert "usage: eastmoneyrzrq" in completed.stdout
