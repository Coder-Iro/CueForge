"""Resolve Windows package dependencies from winget-pkgs manifests."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


GITHUB_API = "https://api.github.com"
RAW_GITHUB = "https://raw.githubusercontent.com"
USER_AGENT = "CueForge-Packager"
STABLE_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True, slots=True)
class DependencyConfig:
    name: str
    package_id: str
    manifest_path: str
    architecture: str
    installer_type: str
    install_subdir: str
    executables: tuple[str, ...]
    license: str


@dataclass(frozen=True, slots=True)
class ResolvedDependency:
    name: str
    package_id: str
    version: str
    manifest_path: str
    architecture: str
    installer_type: str
    url: str
    sha256: str
    archive_name: str
    install_subdir: str
    executables: tuple[str, ...]
    license: str

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "package_id": self.package_id,
            "version": self.version,
            "manifest_path": self.manifest_path,
            "architecture": self.architecture,
            "installer_type": self.installer_type,
            "url": self.url,
            "sha256": self.sha256,
            "archive_name": self.archive_name,
            "install_subdir": self.install_subdir,
            "executables": list(self.executables),
            "license": self.license,
        }


class ResolveError(RuntimeError):
    """Raised when a dependency cannot be resolved from winget-pkgs."""


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    resolved = resolve_dependencies(config, ref=args.ref)
    write_resolved_json(resolved, args.json_out)
    write_inno_include(resolved, args.inno_out)
    print(f"Resolved {len(resolved['dependencies'])} winget dependencies")
    for dependency in resolved["dependencies"]:
        print(f" - {dependency['name']} {dependency['version']} {dependency['sha256']}")
    return 0


def resolve_dependencies(config: dict[str, Any], *, ref: str | None = None) -> dict[str, Any]:
    repository = str(config.get("winget_repository") or "microsoft/winget-pkgs")
    selected_ref = str(ref or config.get("winget_ref") or "master")
    dependencies = [_dependency_config(item) for item in config["dependencies"]]
    resolved = [
        resolve_dependency(dependency, repository=repository, ref=selected_ref)
        for dependency in dependencies
    ]
    return {
        "platform": config["platform"],
        "source": {
            "repository": repository,
            "ref": selected_ref,
            "resolved_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        },
        "dependencies": [dependency.as_json() for dependency in resolved],
    }


def resolve_dependency(dependency: DependencyConfig, *, repository: str, ref: str) -> ResolvedDependency:
    version = latest_stable_version(repository=repository, ref=ref, manifest_path=dependency.manifest_path)
    manifest = fetch_installer_manifest(
        repository=repository,
        ref=ref,
        manifest_path=dependency.manifest_path,
        package_id=dependency.package_id,
        version=version,
    )
    installer = select_installer(manifest, architecture=dependency.architecture)
    manifest_installer_type = str(manifest.get("InstallerType") or "").lower()
    if manifest_installer_type != dependency.installer_type.lower():
        raise ResolveError(
            f"{dependency.package_id} {version} uses installer type "
            f"{manifest_installer_type!r}, expected {dependency.installer_type!r}"
        )

    url = str(installer.get("InstallerUrl") or "")
    sha256 = str(installer.get("InstallerSha256") or "").lower()
    if not url.startswith("https://"):
        raise ResolveError(f"{dependency.package_id} {version} has invalid InstallerUrl: {url!r}")
    if not url.lower().endswith(".zip"):
        raise ResolveError(f"{dependency.package_id} {version} InstallerUrl is not a ZIP archive: {url}")
    if not SHA256_RE.fullmatch(sha256):
        raise ResolveError(f"{dependency.package_id} {version} has invalid InstallerSha256")

    return ResolvedDependency(
        name=dependency.name,
        package_id=dependency.package_id,
        version=version,
        manifest_path=dependency.manifest_path,
        architecture=dependency.architecture,
        installer_type=manifest_installer_type,
        url=url,
        sha256=sha256,
        archive_name=_archive_name(url),
        install_subdir=dependency.install_subdir,
        executables=dependency.executables,
        license=dependency.license,
    )


def latest_stable_version(*, repository: str, ref: str, manifest_path: str) -> str:
    url = f"{GITHUB_API}/repos/{repository}/contents/{manifest_path}?ref={urllib.parse.quote(ref)}"
    entries = fetch_json(url)
    versions = [
        str(entry["name"])
        for entry in entries
        if entry.get("type") == "dir" and STABLE_VERSION_RE.fullmatch(str(entry.get("name") or ""))
    ]
    if not versions:
        raise ResolveError(f"No stable version directories found in {manifest_path}")
    return sorted(versions, key=_version_key)[-1]


def fetch_installer_manifest(
    *,
    repository: str,
    ref: str,
    manifest_path: str,
    package_id: str,
    version: str,
) -> dict[str, Any]:
    url = (
        f"{RAW_GITHUB}/{repository}/{urllib.parse.quote(ref)}/"
        f"{manifest_path}/{version}/{package_id}.installer.yaml"
    )
    return parse_installer_manifest(fetch_text(url))


def select_installer(manifest: dict[str, Any], *, architecture: str) -> dict[str, str]:
    for installer in manifest.get("Installers", []):
        if str(installer.get("Architecture") or "").lower() == architecture.lower():
            return installer
    raise ResolveError(f"No {architecture} installer found for {manifest.get('PackageIdentifier')}")


def parse_installer_manifest(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {"Installers": []}
    section: str | None = None
    current_installer: dict[str, str] | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        if section == "Installers" and line.startswith("- "):
            current_installer = {}
            result["Installers"].append(current_installer)
            rest = line[2:].strip()
            if rest:
                key, value = _split_yaml_key(rest)
                if value is not None:
                    current_installer[key] = _yaml_scalar(value)
            continue

        if indent == 0:
            section = None
            current_installer = None
            key, value = _split_yaml_key(line)
            if key == "Installers":
                section = "Installers"
            elif value is not None:
                result[key] = _yaml_scalar(value)
            continue

        if section == "Installers" and current_installer is not None:
            key, value = _split_yaml_key(line)
            if value is not None:
                current_installer[key] = _yaml_scalar(value)

    if not result["Installers"]:
        raise ResolveError("Installer manifest did not contain Installers")
    return result


def write_resolved_json(resolved: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(resolved, indent=2) + "\n", encoding="utf-8")


def write_inno_include(resolved: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dependencies = resolved["dependencies"]
    lines = [
        "// Generated by scripts/resolve_winget_dependencies.py. Do not edit by hand.",
        "",
        "procedure AddDependencyDownloads;",
        "begin",
    ]
    for dependency in dependencies:
        lines.extend(
            [
                "  DownloadPage.Add(",
                f"    '{_inno_quote(dependency['url'])}',",
                f"    '{_inno_quote(dependency['archive_name'])}',",
                f"    '{_inno_quote(dependency['sha256'])}'",
                "  );",
            ]
        )
    lines.extend(
        [
            "end;",
            "",
            "function ExtractAllDependencies: Boolean;",
            "begin",
            "  Result :=",
        ]
    )
    extract_calls = [
        (
            f"    ExtractDependencyArchive('{_inno_quote(dependency['name'])}', "
            f"'{_inno_quote(dependency['archive_name'])}', "
            f"'{_inno_quote(dependency['install_subdir'])}')"
        )
        for dependency in dependencies
    ]
    for index, call in enumerate(extract_calls):
        suffix = ";" if index == len(extract_calls) - 1 else " and"
        lines.append(f"{call}{suffix}")
    lines.append("end;")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fetch_json(url: str) -> Any:
    return json.loads(fetch_text(url))


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        raise ResolveError(f"HTTP {exc.code} while fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise ResolveError(f"Failed to fetch {url}: {exc}") from exc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("packaging/dependencies.windows-x64.json"))
    parser.add_argument("--json-out", type=Path, default=Path("build/dependencies.windows-x64.resolved.json"))
    parser.add_argument("--inno-out", type=Path, default=Path("build/dependencies.windows-x64.iss"))
    parser.add_argument("--ref", default=None)
    return parser.parse_args(argv)


def _dependency_config(item: dict[str, Any]) -> DependencyConfig:
    return DependencyConfig(
        name=str(item["name"]),
        package_id=str(item["package_id"]),
        manifest_path=str(item["manifest_path"]).strip("/"),
        architecture=str(item.get("architecture") or "x64"),
        installer_type=str(item.get("installer_type") or "zip"),
        install_subdir=str(item["install_subdir"]),
        executables=tuple(str(executable) for executable in item["executables"]),
        license=str(item["license"]),
    )


def _split_yaml_key(line: str) -> tuple[str, str | None]:
    if ":" not in line:
        return line, None
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _archive_name(url: str) -> str:
    return Path(urllib.parse.urlparse(url).path).name


def _inno_quote(value: str) -> str:
    return value.replace("'", "''")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
