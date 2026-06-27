import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_resolver() -> ModuleType:
    path = ROOT / "scripts" / "resolve_winget_dependencies.py"
    spec = importlib.util.spec_from_file_location("resolve_winget_dependencies", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_report_writer() -> ModuleType:
    path = ROOT / "scripts" / "write_release_report.py"
    spec = importlib.util.spec_from_file_location("write_release_report", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_windows_dependency_config_uses_winget_manifest_sources() -> None:
    payload = json.loads((ROOT / "packaging" / "dependencies.windows-x64.json").read_text(encoding="utf-8"))

    assert payload["platform"] == "windows-x64"
    assert payload["winget_repository"] == "microsoft/winget-pkgs"
    assert {item["package_id"] for item in payload["dependencies"]} == {
        "DenoLand.Deno",
        "AcoustID.Chromaprint",
        "Gyan.FFmpeg.Shared",
    }
    for dependency in payload["dependencies"]:
        assert dependency["manifest_path"].startswith("manifests/")
        assert dependency["architecture"] == "x64"
        assert dependency["installer_type"] == "zip"
        assert dependency["install_subdir"]
        assert dependency["executables"]
        assert "url" not in dependency
        assert "sha256" not in dependency
        assert "version" not in dependency


def test_online_installer_uses_generated_dependency_include() -> None:
    script = (ROOT / "packaging" / "cueforge-online.iss").read_text(encoding="utf-8")

    assert "#include DependencyInclude" in script
    assert "AddDependencyDownloads;" in script
    assert "ExtractAllDependencies" in script
    assert "Failed to download external dependencies (ffmpeg, fpcalc, and Deno)" in script
    assert "github.com/denoland/deno/releases" not in script
    assert "github.com/acoustid/chromaprint/releases" not in script
    assert "github.com/GyanD/codexffmpeg/releases" not in script


def test_pyinstaller_spec_bundles_ytmusicapi_locales() -> None:
    spec = (ROOT / "packaging" / "cueforge.spec").read_text(encoding="utf-8")

    assert "collect_data_files" in spec
    assert '"ytmusicapi"' in spec
    assert "locales/**/*" in spec
    assert 'ROOT / "config" / "google_oauth_client.json"' in spec
    assert "datas.append((str(oauth_client_config), \"config\"))" in spec


def test_packaging_script_times_out_packaged_diagnostics() -> None:
    script = (ROOT / "scripts" / "package_windows.ps1").read_text(encoding="utf-8")

    assert "function Invoke-PackagedDiagnostics" in script
    assert "function Invoke-PackagedCommand" in script
    assert "Wait-Process" in script
    assert "-WindowStyle Hidden" in script
    assert "timed out after $TimeoutSeconds seconds" in script


def test_packaging_script_runs_release_checks_in_fixed_order() -> None:
    script = (ROOT / "scripts" / "package_windows.ps1").read_text(encoding="utf-8")

    full_pytest = 'Invoke-Native $Python @("-m", "pytest")'
    gui_smoke = 'Invoke-Native $Python @("-m", "cueforge", "--smoke-gui")'
    fixture_suite = 'Invoke-Native $Python @("-m", "pytest", "tests\\test_metadata_regressions.py")'
    packaged_diagnose = 'Invoke-PackagedCommand -Executable $Executable -Arguments @("--diagnose-file", $DiagnosticsPath)'
    packaged_smoke = 'Invoke-PackagedCommand -Executable $Executable -Arguments @("--smoke-gui")'

    assert script.index(full_pytest) < script.index(gui_smoke) < script.index(fixture_suite)
    assert script.index(packaged_diagnose) < script.index(packaged_smoke)


def test_packaging_script_writes_release_report() -> None:
    script = (ROOT / "scripts" / "package_windows.ps1").read_text(encoding="utf-8")

    assert "scripts\\write_release_report.py" in script
    assert "windows-x64-release-report.json" in script
    assert "--diagnostics-file" in script
    assert "--dependencies-json" in script
    assert "--installer" in script
    assert "--checksum-file" in script


def test_packaging_script_uses_dependency_lock_when_present() -> None:
    script = (ROOT / "scripts" / "package_windows.ps1").read_text(encoding="utf-8")

    assert "dependencies.windows-x64.lock.json" in script
    assert "--lock-file" in script


def test_third_party_notices_are_available() -> None:
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert "Third-Party Notices" in notices


def test_release_report_writer_includes_dependency_hashes_and_diagnostics(tmp_path: Path) -> None:
    writer = _load_report_writer()
    dependencies = tmp_path / "dependencies.json"
    diagnostics = tmp_path / "diagnostics.txt"
    installer = tmp_path / "setup.exe"
    checksum = tmp_path / "setup.exe.sha256"
    output = tmp_path / "release-report.json"
    dependencies.write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-06-07T00:00:00+00:00",
                "dependencies": [
                    {
                        "name": "ffmpeg",
                        "package_id": "Gyan.FFmpeg.Shared",
                        "version": "1.2.3",
                        "url": "https://example.invalid/ffmpeg.zip",
                        "sha256": "a" * 64,
                        "install_subdir": "ffmpeg",
                        "executables": ["ffmpeg.exe", "ffprobe.exe"],
                        "license": "LGPL/GPL",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    diagnostics.write_text("diagnostics ok\n", encoding="utf-8")
    installer.write_bytes(b"installer")
    checksum.write_text("checksum  setup.exe\n", encoding="ascii")

    report = writer.write_release_report(
        version="1.0.0",
        output=output,
        diagnostics_file=diagnostics,
        dependencies_json=dependencies,
        installer=installer,
        checksum_file=checksum,
        tests_skipped=False,
    )

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved == report
    assert saved["verification_order"] == writer.VERIFICATION_ORDER
    assert saved["dependencies"]["dependencies"][0]["sha256"] == "a" * 64
    assert saved["packaged_results"][0]["output"]["sha256"]
    assert saved["installer"]["sha256"]
    assert saved["notices"]["third_party_notice"] == "THIRD_PARTY_NOTICES.md"


def test_resolver_selects_latest_stable_x64_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _load_resolver()

    def fake_fetch_json(url: str):
        assert "manifests/vendor/tool" in url
        return [
            {"name": "1.9.0", "type": "dir"},
            {"name": "2.0.0-rc1", "type": "dir"},
            {"name": "2.0.0", "type": "dir"},
            {"name": ".validation", "type": "file"},
        ]

    def fake_fetch_text(url: str):
        assert url.endswith("/manifests/vendor/tool/2.0.0/Vendor.Tool.installer.yaml")
        return """
PackageIdentifier: Vendor.Tool
PackageVersion: 2.0.0
InstallerType: zip
Installers:
- Architecture: arm64
  InstallerUrl: https://example.invalid/tool-arm64.zip
  InstallerSha256: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
- Architecture: x64
  InstallerUrl: https://example.invalid/tool-x64.zip
  InstallerSha256: BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
ManifestType: installer
"""

    monkeypatch.setattr(resolver, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(resolver, "fetch_text", fake_fetch_text)

    config = {
        "platform": "windows-x64",
        "winget_repository": "example/repo",
        "winget_ref": "master",
        "dependencies": [
            {
                "name": "tool",
                "package_id": "Vendor.Tool",
                "manifest_path": "manifests/vendor/tool",
                "architecture": "x64",
                "installer_type": "zip",
                "install_subdir": "tool",
                "executables": ["tool.exe"],
                "license": "MIT",
            }
        ],
    }

    resolved = resolver.resolve_dependencies(config)
    dependency = resolved["dependencies"][0]

    assert dependency["version"] == "2.0.0"
    assert dependency["url"] == "https://example.invalid/tool-x64.zip"
    assert dependency["sha256"] == "b" * 64
    assert dependency["archive_name"] == "tool-x64.zip"


def test_resolver_can_emit_from_lock_file_without_fetching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _load_resolver()
    config = tmp_path / "dependencies.json"
    lock = tmp_path / "dependencies.lock.json"
    json_out = tmp_path / "resolved.json"
    inno_out = tmp_path / "resolved.iss"
    config.write_text(json.dumps({"platform": "windows-x64", "dependencies": []}), encoding="utf-8")
    lock.write_text(
        json.dumps(
            {
                "platform": "windows-x64",
                "dependencies": [
                    {
                        "name": "tool",
                        "package_id": "Vendor.Tool",
                        "version": "1.0.0",
                        "manifest_path": "manifests/vendor/tool",
                        "architecture": "x64",
                        "installer_type": "zip",
                        "url": "https://example.invalid/tool.zip",
                        "sha256": "a" * 64,
                        "archive_name": "tool.zip",
                        "install_subdir": "tool",
                        "executables": ["tool.exe"],
                        "license": "MIT",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(resolver, "resolve_dependencies", lambda config, ref=None: (_ for _ in ()).throw(AssertionError("fetch")))

    assert resolver.main(
        [
            "--config",
            str(config),
            "--lock-file",
            str(lock),
            "--json-out",
            str(json_out),
            "--inno-out",
            str(inno_out),
        ]
    ) == 0

    assert json.loads(json_out.read_text(encoding="utf-8"))["dependencies"][0]["version"] == "1.0.0"
    assert "tool.zip" in inno_out.read_text(encoding="utf-8")


def test_resolver_rejects_missing_x64_installer() -> None:
    resolver = _load_resolver()
    manifest = resolver.parse_installer_manifest(
        """
PackageIdentifier: Vendor.Tool
PackageVersion: 1.0.0
InstallerType: zip
Installers:
- Architecture: arm64
  InstallerUrl: https://example.invalid/tool-arm64.zip
  InstallerSha256: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
"""
    )

    with pytest.raises(resolver.ResolveError, match="No x64 installer"):
        resolver.select_installer(manifest, architecture="x64")


def test_inno_include_generation(tmp_path: Path) -> None:
    resolver = _load_resolver()
    output = tmp_path / "dependencies.iss"
    resolved = {
        "dependencies": [
            {
                "name": "tool",
                "url": "https://example.invalid/tool.zip",
                "sha256": "a" * 64,
                "archive_name": "tool.zip",
                "install_subdir": "tool",
            },
            {
                "name": "quote'tool",
                "url": "https://example.invalid/quote.zip",
                "sha256": "b" * 64,
                "archive_name": "quote.zip",
                "install_subdir": "quote",
            },
        ]
    }

    resolver.write_inno_include(resolved, output)
    script = output.read_text(encoding="utf-8")

    assert "procedure AddDependencyDownloads;" in script
    assert "function ExtractAllDependencies: Boolean;" in script
    assert "DownloadPage.Add(" in script
    assert re.search(r"ExtractDependencyArchive\('tool', 'tool\.zip', 'tool'\)", script)
    assert "ExtractDependencyArchive('quote''tool', 'quote.zip', 'quote')" in script
