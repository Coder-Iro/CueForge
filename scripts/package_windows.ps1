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

function Invoke-PackagedCommand {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$Label,
        [int]$TimeoutSeconds = 60
    )

    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru -WindowStyle Hidden
    Wait-Process -Id $process.Id -Timeout $TimeoutSeconds -ErrorAction SilentlyContinue
    $runningProcess = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
    if ($runningProcess) {
        Stop-Process -Id $process.Id -Force
        throw "$Label timed out after $TimeoutSeconds seconds"
    }
    if ($process.ExitCode -ne 0) {
        throw "$Label failed with exit code $($process.ExitCode)"
    }
}

function Invoke-PackagedDiagnostics {
    param(
        [string]$Executable,
        [string]$DiagnosticsPath,
        [int]$TimeoutSeconds = 60
    )

    Invoke-PackagedCommand -Executable $Executable -Arguments @("--diagnose-file", $DiagnosticsPath) -Label "Packaged diagnose" -TimeoutSeconds $TimeoutSeconds
    Invoke-PackagedCommand -Executable $Executable -Arguments @("--smoke-gui") -Label "Packaged smoke-gui" -TimeoutSeconds $TimeoutSeconds
}

Push-Location $Root
try {
    if (-not $SkipTests) {
        Invoke-Native $Python @("-m", "pytest")
        Invoke-Native $Python @("-m", "cueforge", "--smoke-gui")
        Invoke-Native $Python @("-m", "pytest", "tests\test_metadata_regressions.py")
    }

    Invoke-Native $Python @("-m", "pip", "install", "-e", ".[packaging]")

    Remove-BuildPath (Join-Path $Root "dist\CueForge")
    Remove-BuildPath (Join-Path $Root "build\cueforge")
    New-Item -ItemType Directory -Force -Path "release" | Out-Null
    New-Item -ItemType Directory -Force -Path "build" | Out-Null

    Invoke-Native $Python @("-m", "PyInstaller", "--noconfirm", "packaging\cueforge.spec")

    $packagedExe = Join-Path $Root "dist\CueForge\CueForge.exe"
    if (-not (Test-Path -LiteralPath $packagedExe)) {
        throw "Expected PyInstaller executable was not produced: $packagedExe"
    }
    $diagnosticsPath = Join-Path $Root "build\packaged-diagnostics.txt"
    Invoke-PackagedDiagnostics -Executable $packagedExe -DiagnosticsPath $diagnosticsPath
    if (-not (Test-Path -LiteralPath $diagnosticsPath)) {
        throw "Packaged diagnostics file was not produced: $diagnosticsPath"
    }
    Write-Host "Packaged diagnostics: $diagnosticsPath"

    $resolvedDependencyJson = ""
    $installer = ""
    $checksumPath = ""
    if (-not $SkipInstaller) {
        $resolvedDependencyInno = Join-Path $Root "build\dependencies.windows-x64.iss"
        $resolvedDependencyJson = Join-Path $Root "build\dependencies.windows-x64.resolved.json"
        Invoke-Native $Python @(
            "scripts\resolve_winget_dependencies.py",
            "--config",
            "packaging\dependencies.windows-x64.json",
            "--json-out",
            $resolvedDependencyJson,
            "--inno-out",
            $resolvedDependencyInno
        )

        $dependencyReport = Join-Path $Root "release\CueForge-$Version-windows-x64-dependencies.json"

        $iscc = Resolve-InnoCompiler
        Invoke-Native $iscc @(
            "packaging\cueforge-online.iss",
            "/DAppVersion=$Version",
            "/DOutputDir=..\release",
            "/DDistDir=..\dist\CueForge",
            "/DDependencyInclude=..\build\dependencies.windows-x64.iss"
        )

        $installer = Join-Path $Root "release\CueForge-$Version-windows-x64-online-setup.exe"
        if (-not (Test-Path -LiteralPath $installer)) {
            throw "Expected installer was not produced: $installer"
        }

        $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $installer
        $checksumPath = "$installer.sha256"
        "$($hash.Hash.ToLowerInvariant())  $(Split-Path -Leaf $installer)" | Set-Content -Path $checksumPath -Encoding ascii
        Copy-Item -LiteralPath $resolvedDependencyJson -Destination $dependencyReport -Force
        Write-Host "Installer: $installer"
        Write-Host "SHA256: $($hash.Hash.ToLowerInvariant())"
        Write-Host "Dependencies: $dependencyReport"
    }

    $releaseReport = Join-Path $Root "release\CueForge-$Version-windows-x64-release-report.json"
    $reportArgs = @(
        "scripts\write_release_report.py",
        "--version",
        $Version,
        "--diagnostics-file",
        $diagnosticsPath,
        "--output",
        $releaseReport
    )
    if ($SkipTests) {
        $reportArgs += "--tests-skipped"
    }
    if ($resolvedDependencyJson -and (Test-Path -LiteralPath $resolvedDependencyJson)) {
        $reportArgs += @("--dependencies-json", $resolvedDependencyJson)
    }
    if ($installer -and (Test-Path -LiteralPath $installer)) {
        $reportArgs += @("--installer", $installer)
    }
    if ($checksumPath -and (Test-Path -LiteralPath $checksumPath)) {
        $reportArgs += @("--checksum-file", $checksumPath)
    }
    Invoke-Native $Python $reportArgs
    Write-Host "Release report: $releaseReport"
}
finally {
    Pop-Location
}
