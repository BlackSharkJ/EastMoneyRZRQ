#Requires -Version 5.1
<#
.SYNOPSIS
    一键对齐 EastMoneyRZRQ 的开发环境.

.DESCRIPTION
    依次完成: 检查 uv, 同步依赖, 安装 playwright chromium, 安装 pre-commit 钩子,
    最后跑一遍单元测试. 重复执行是安全的.

.EXAMPLE
    pwsh -File scripts/setup_tools.ps1

.EXAMPLE
    pwsh -File scripts/setup_tools.ps1 -SkipTests
#>
[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $ProjectRoot

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-Command {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Hint
    )
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "找不到命令 $Name. $Hint"
    }
    Write-Host "[OK] $Name 已安装" -ForegroundColor Green
}

function Assert-PythonVersion {
    $versionText = & uv run --no-sync python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
    if ($versionText -ne "3.13") {
        throw "当前虚拟环境是 Python $versionText, 本项目要求 3.13"
    }
    Write-Host "[OK] Python $versionText" -ForegroundColor Green
}

try {
    Write-Step "检查前置工具"
    Assert-Command -Name "uv" -Hint "安装方式: https://docs.astral.sh/uv/getting-started/installation/"

    Write-Step "同步依赖 (uv sync)"
    uv sync

    Write-Step "检查 Python 版本"
    Assert-PythonVersion

    Write-Step "安装 chromium (playwright 截图用)"
    uv run playwright install chromium

    Write-Step "安装 pre-commit 钩子"
    uv run pre-commit install

    if (-not $SkipTests) {
        Write-Step "运行单元测试"
        uv run pytest tests/unit -q
    }

    Write-Host ""
    Write-Host "环境已就绪. 运行方式:" -ForegroundColor Green
    Write-Host "  Start.bat                 # 生成报告并推送企业微信"
    Write-Host "  Start.bat --no-send       # 只生成图片"
    Write-Host "  uv run eastmoneyrzrq --help"
    Write-Host ""
    Write-Host "记得复制 .env.example 为 .env 并填入 WECHAT_WEBHOOK_KEY"
}
finally {
    Pop-Location
}
