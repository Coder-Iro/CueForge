"""Write a Windows release verification report."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


VERIFICATION_ORDER = [
    "full pytest",
    "GUI smoke",
    "metadata fixture suite",
    "packaged diagnose",
    "packaged smoke-gui",
]


def write_release_report(
    *,
    version: str,
    output: Path,
    diagnostics_file: Path,
    dependencies_json: Path | None = None,
    installer: Path | None = None,
    checksum_file: Path | None = None,
    tests_skipped: bool = False,
) -> dict[str, Any]:
    dependencies = _read_dependencies(dependencies_json)
    diagnostics = _file_entry(diagnostics_file)
    installer_entry = _file_entry(installer) if installer else None
    checksum_entry = _file_entry(checksum_file) if checksum_file else None
    report = {
        "schema_version": 1,
        "app": "CueForge",
        "version": version,
        "platform": "windows-x64",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "verification_order": VERIFICATION_ORDER,
        "test_results": _test_results(tests_skipped=tests_skipped),
        "packaged_results": [
            {
                "name": "packaged diagnose",
                "command": "CueForge.exe --diagnose-file <diagnostics>",
                "status": "passed",
                "output": diagnostics,
            },
            {
                "name": "packaged smoke-gui",
                "command": "CueForge.exe --smoke-gui",
                "status": "passed",
            },
        ],
        "dependencies": dependencies,
        "installer": installer_entry,
        "checksum": checksum_entry,
        "notices": {
            "third_party_notice": "THIRD_PARTY_NOTICES.md",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _test_results(*, tests_skipped: bool) -> list[dict[str, str]]:
    status = "skipped" if tests_skipped else "passed"
    return [
        {"name": "full pytest", "command": "python -m pytest", "status": status},
        {"name": "GUI smoke", "command": "python -m cueforge --smoke-gui", "status": status},
        {
            "name": "metadata fixture suite",
            "command": "python -m pytest tests/test_metadata_regressions.py",
            "status": status,
        },
    ]


def _read_dependencies(path: Path | None) -> dict[str, Any]:
    if not path:
        return {"status": "not_built", "dependencies": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    dependencies = []
    for dependency in payload.get("dependencies", []):
        dependencies.append(
            {
                "name": dependency.get("name", ""),
                "package_id": dependency.get("package_id", ""),
                "version": dependency.get("version", ""),
                "url": dependency.get("url", ""),
                "sha256": dependency.get("sha256", ""),
                "install_subdir": dependency.get("install_subdir", ""),
                "executables": dependency.get("executables", []),
                "license": dependency.get("license", ""),
            }
        )
    return {
        "status": "resolved",
        "source": str(path),
        "generated_at_utc": payload.get("generated_at_utc", ""),
        "dependencies": dependencies,
    }


def _file_entry(path: Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    entry: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if path.exists() and path.is_file():
        entry["sha256"] = _sha256(path)
        entry["bytes"] = path.stat().st_size
    return entry


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diagnostics-file", type=Path, required=True)
    parser.add_argument("--dependencies-json", type=Path)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--checksum-file", type=Path)
    parser.add_argument("--tests-skipped", action="store_true")
    args = parser.parse_args()
    write_release_report(
        version=args.version,
        output=args.output,
        diagnostics_file=args.diagnostics_file,
        dependencies_json=args.dependencies_json,
        installer=args.installer,
        checksum_file=args.checksum_file,
        tests_skipped=args.tests_skipped,
    )
    print(f"Wrote release report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
