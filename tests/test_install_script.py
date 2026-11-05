"""Exercise the PowerShell installer without network access or user PATH writes."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows installer")
def test_install_first_repeat_path_and_failure(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is unavailable")
    installer = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"
    harness = tmp_path / "installer-test.ps1"
    harness.write_text(r'''
param($Installer, $Work)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Installer syntax errors' }
$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}, $false) |
    ForEach-Object { Invoke-Expression $_.Extent.Text }
$script:saved = 'C:\Existing'
$script:writes = 0
$script:calls = [Collections.Generic.List[object]]::new()
$script:fail = $false
$script:exists = $false
function Get-KernelCoderUserPath { return $script:saved }
function Set-KernelCoderUserPath([string]$Value) { $script:saved = $Value; $script:writes++ }
function Test-Path { param($LiteralPath, $PathType) return $script:exists }
function Invoke-KernelCoderPython([string]$Executable, [string[]]$Arguments, [string]$FailureMessage) {
    if ($script:fail) { throw $FailureMessage }
    $script:calls.Add(@{exe=$Executable; arguments=$Arguments})
}
$repo = Join-Path $Work 'repo space 中文'
Install-KernelCoder $repo 'python'
if ($script:calls.Count -ne 4) { throw 'First install did not validate Python/create venv/install/verify' }
if ($script:calls[2].arguments[-1] -cne $repo) { throw 'Repository path was split or changed' }
$target = Join-Path $repo '.venv\Scripts'
if (($env:Path -split ';')[0] -ine $target) { throw 'Current terminal PATH missing' }
if ($script:writes -ne 1) { throw 'User PATH not updated' }
$script:exists = $true
$script:calls.Clear()
Install-KernelCoder $repo 'python'
if ($script:calls.Count -ne 2) { throw 'Existing venv was not reused' }
if ($script:writes -ne 1) { throw 'User PATH duplicated' }
$before = $env:Path
$script:fail = $true
$failed = $false
try { Install-KernelCoder (Join-Path $Work 'failed repo') 'python' } catch { $failed = $true }
if (-not $failed) { throw 'Failure was ignored' }
if ($env:Path -cne $before -or $script:writes -ne 1) { throw 'Failed install changed PATH' }
# Also check a failed native command propagates from the real wrapper.
$native = $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Invoke-KernelCoderPython'}, $false)[0]
Invoke-Expression $native.Extent.Text
$failed = $false
try { Invoke-KernelCoderPython $env:ComSpec @('/c', 'exit', '3') 'expected-native-failure' }
catch { $failed = $_.Exception.Message -eq 'expected-native-failure' }
if (-not $failed) { throw 'Native failure was ignored' }
Write-Output 'installer scenarios passed'
''', encoding="utf-8-sig")
    result = subprocess.run([shell, "-NoProfile", "-File", str(harness), str(installer), str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "installer scenarios passed" in result.stdout
