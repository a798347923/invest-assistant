<#
.SYNOPSIS
    invest-assistant 服务安装脚本（NSSM 优先，自动回退 Windows 计划任务）。

.DESCRIPTION
    安装 / 卸载 / 查看状态，支持 -DryRun 预览将执行的操作。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Action install -DryRun
    powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Action install
    powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Action uninstall
    powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1 -Action status
#>
param(
    [ValidateSet("install", "uninstall", "status")]
    [string]$Action = "install",
    [ValidateSet("auto", "nssm", "task")]
    [string]$Mode = "auto",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonW = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ServiceName = "InvestAssistant"
$TaskName = "InvestAssistant"
$AppArgs = "-m invest_assistant serve"

function Get-NssmPath {
    $cmd = Get-Command nssm -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @("$env:ProgramFiles\nssm\nssm.exe", "${env:ProgramFiles(x86)}\nssm\nssm.exe")
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}

function Resolve-Mode {
    if ($Mode -ne "auto") { return $Mode }
    if (Get-NssmPath) { return "nssm" }
    return "task"
}

function Invoke-Step {
    param([string]$Description, [scriptblock]$Command)
    Write-Host "==> $Description"
    if ($DryRun) {
        Write-Host "    [DryRun] $($Command.ToString().Trim())"
        return
    }
    & $Command
}

if (-not (Test-Path $Python)) {
    throw "未找到虚拟环境 Python：$Python（请先执行 pip install -e `".[dev]`"）"
}

$resolvedMode = Resolve-Mode
Write-Host "项目根目录: $ProjectRoot"
Write-Host "运行模式:   $resolvedMode"
Write-Host "动作:       $Action"

switch ($Action) {
    "install" {
        if ($resolvedMode -eq "nssm") {
            $nssm = Get-NssmPath
            Invoke-Step "安装服务（NSSM）" { & $nssm install $ServiceName $PythonW $AppArgs }
            Invoke-Step "设置工作目录"     { & $nssm set $ServiceName AppDirectory $ProjectRoot }
            Invoke-Step "设置描述"         { & $nssm set $ServiceName Description "AI 投资分析助手（个人自用）" }
            Invoke-Step "崩溃自动重启"     { & $nssm set $ServiceName AppExit Default Restart }
            Invoke-Step "开机自启"         { & $nssm set $ServiceName Start SERVICE_AUTO_START }
            Invoke-Step "启动服务"         { & $nssm start $ServiceName }
        }
        else {
            $tr = "`"$PythonW`" $AppArgs"
            Invoke-Step "注册计划任务（登录时启动）" { schtasks /Create /TN $TaskName /SC ONLOGON /TR $tr /F }
            Invoke-Step "立即启动计划任务"          { schtasks /Run /TN $TaskName }
        }
        Write-Host "完成。卸载：-Action uninstall"
    }
    "uninstall" {
        if ($resolvedMode -eq "nssm") {
            $nssm = Get-NssmPath
            Invoke-Step "停止服务" { & $nssm stop $ServiceName }
            Invoke-Step "删除服务" { & $nssm remove $ServiceName confirm }
        }
        else {
            Invoke-Step "停止任务" { schtasks /End /TN $TaskName }
            Invoke-Step "删除任务" { schtasks /Delete /TN $TaskName /F }
        }
    }
    "status" {
        if ($resolvedMode -eq "nssm") {
            $nssm = Get-NssmPath
            & $nssm status $ServiceName
        }
        else {
            schtasks /Query /TN $TaskName /FO LIST
        }
    }
}
