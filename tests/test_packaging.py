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
    script = (ROOT / "packaging" / "ytdj-online.iss").read_text(encoding="utf-8")

    assert "#include DependencyInclude" in script
    assert "AddDependencyDownloads;" in script
    assert "ExtractAllDependencies" in script
    assert "github.com/denoland/deno/releases" not in script
    assert "github.com/acoustid/chromaprint/releases" not in script
    assert "github.com/GyanD/codexffmpeg/releases" not in script


def test_pyinstaller_spec_bundles_ytmusicapi_locales() -> None:
    spec = (ROOT / "packaging" / "ytdj.spec").read_text(encoding="utf-8")

    assert "collect_data_files" in spec
    assert '"ytmusicapi"' in spec
    assert "locales/**/*" in spec


def test_packaging_script_times_out_packaged_diagnostics() -> None:
    script = (ROOT / "scripts" / "package_windows.ps1").read_text(encoding="utf-8")

    assert "function Invoke-PackagedDiagnostics" in script
    assert "Wait-Process" in script
    assert "Packaged diagnostics timed out" in script


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
