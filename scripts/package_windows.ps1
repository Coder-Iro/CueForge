param(
    [string]$Version = "0.1.0-alpha1",
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

function Remove-BuildPath {
    param([string]$Path)
    $resolvedRoot = [System.IO.Path]::GetFullPath("$Root\")
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside repository: $fullPath"
    }
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
}

function Resolve-InnoCompiler {
    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    throw "ISCC.exe was not found. Install Inno Setup 6.5+ or pass -SkipInstaller to build only the PyInstaller app."
}

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

Push-Location $Root
try {
    $dependencyLock = Get-Content -Raw -Path "packaging\dependencies.windows-x64.json" | ConvertFrom-Json
    Write-Host "Packaging dependencies locked for $($dependencyLock.platform):"
    foreach ($dependency in $dependencyLock.dependencies) {
        Write-Host " - $($dependency.name) $($dependency.version) $($dependency.sha256)"
    }

    if (-not $SkipTests) {
        Invoke-Native $Python @("-m", "pytest")
    }

    Invoke-Native $Python @("-m", "pip", "install", "-e", ".[packaging]")

    Remove-BuildPath (Join-Path $Root "dist\YT-DJ")
    Remove-BuildPath (Join-Path $Root "build\ytdj")
    New-Item -ItemType Directory -Force -Path "release" | Out-Null

    Invoke-Native $Python @("-m", "PyInstaller", "--noconfirm", "packaging\ytdj.spec")

    $packagedExe = Join-Path $Root "dist\YT-DJ\YT-DJ.exe"
    if (-not (Test-Path -LiteralPath $packagedExe)) {
        throw "Expected PyInstaller executable was not produced: $packagedExe"
    }
    $diagnosticsPath = Join-Path $Root "build\packaged-diagnostics.txt"
    $diagnosticsProcess = Start-Process -FilePath $packagedExe -ArgumentList @("--diagnose-file", $diagnosticsPath, "--smoke-gui") -PassThru -Wait
    if ($diagnosticsProcess.ExitCode -ne 0) {
        throw "Packaged diagnostics failed with exit code $($diagnosticsProcess.ExitCode)"
    }
    if (-not (Test-Path -LiteralPath $diagnosticsPath)) {
        throw "Packaged diagnostics file was not produced: $diagnosticsPath"
    }
    Write-Host "Packaged diagnostics: $diagnosticsPath"

    if (-not $SkipInstaller) {
        $iscc = Resolve-InnoCompiler
        Invoke-Native $iscc @("packaging\ytdj-online.iss", "/DAppVersion=$Version", "/DOutputDir=..\release", "/DDistDir=..\dist\YT-DJ")

        $installer = Join-Path $Root "release\YT-DJ-$Version-windows-x64-online-setup.exe"
        if (-not (Test-Path -LiteralPath $installer)) {
            throw "Expected installer was not produced: $installer"
        }

        $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $installer
        $checksumPath = "$installer.sha256"
        "$($hash.Hash.ToLowerInvariant())  $(Split-Path -Leaf $installer)" | Set-Content -Path $checksumPath -Encoding ascii
        Write-Host "Installer: $installer"
        Write-Host "SHA256: $($hash.Hash.ToLowerInvariant())"
    }
}
finally {
    Pop-Location
}
