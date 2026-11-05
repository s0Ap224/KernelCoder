# Run from a cloned repository: & .\scripts\install.ps1
[CmdletBinding()]
param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'

function Get-KernelCoderUserPath {
    [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Set-KernelCoderUserPath([string]$Value) {
    [Environment]::SetEnvironmentVariable('Path', $Value, 'User')
}

function Invoke-KernelCoderPython([string]$Executable, [string[]]$Arguments, [string]$FailureMessage) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
}

function Add-KernelCoderPath([string]$Scripts) {
    $target = [IO.Path]::GetFullPath($Scripts).TrimEnd('\', '/')
    $userPath = Get-KernelCoderUserPath
    $entries = @($userPath -split ';' | Where-Object { $_ })
    $alreadyPresent = @($entries | Where-Object {
        [Environment]::ExpandEnvironmentVariables($_.Trim('"')).TrimEnd('\', '/') -ieq $target
    }).Count -gt 0
    if (-not $alreadyPresent) {
        Set-KernelCoderUserPath (($entries + $target) -join ';')
    }
    # Prefer this installation in the current PowerShell process as well.
    $current = @($env:Path -split ';' | Where-Object {
        $_ -and $_.Trim('"').TrimEnd('\', '/') -ine $target
    })
    $env:Path = (@($target) + $current) -join ';'
}

function Install-KernelCoder([string]$RepositoryRoot, [string]$PythonCommand) {
    $venvPython = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Invoke-KernelCoderPython $PythonCommand @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)') `
            'Python 3.10+ is required. Install Python and retry.'
        Invoke-KernelCoderPython $PythonCommand @('-m', 'venv', (Join-Path $RepositoryRoot '.venv')) `
            'Virtual environment creation failed. Check your Python installation.'
    }
    Invoke-KernelCoderPython $venvPython @('-m', 'pip', 'install', '-e', $RepositoryRoot) `
        'KernelCoder installation failed. Check network access and pip output above.'
    Invoke-KernelCoderPython $venvPython @('-m', 'kernelcoder', '--version') `
        'KernelCoder verification failed; PATH was not changed.'
    Add-KernelCoderPath (Join-Path $RepositoryRoot '.venv\Scripts')
    Write-Host 'Installed. Run: kernelcoder init'
    Write-Host 'This terminal is ready. Reopen other terminals to refresh their PATH.'
    Write-Host 'Keep this repository in place: the editable installation points to it.'
}

try {
    $repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    Install-KernelCoder $repository $Python
}
catch {
    Write-Error ("Install failed: " + $_.Exception.Message) -ErrorAction Continue
    exit 1
}
