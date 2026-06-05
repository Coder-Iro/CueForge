import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_dependency_lock_is_well_formed() -> None:
    payload = json.loads((ROOT / "packaging" / "dependencies.windows-x64.json").read_text(encoding="utf-8"))

    assert payload["platform"] == "windows-x64"
    assert {item["name"] for item in payload["dependencies"]} == {"deno", "chromaprint-fpcalc", "ffmpeg"}
    for dependency in payload["dependencies"]:
        assert dependency["url"].startswith("https://")
        assert re.fullmatch(r"[0-9a-f]{64}", dependency["sha256"])
        assert dependency["size"] > 0
        assert dependency["archive_name"].endswith(".zip")
        assert dependency["install_subdir"]
        assert dependency["executables"]


def test_online_installer_uses_locked_dependency_urls_and_hashes() -> None:
    payload = json.loads((ROOT / "packaging" / "dependencies.windows-x64.json").read_text(encoding="utf-8"))
    script = (ROOT / "packaging" / "ytdj-online.iss").read_text(encoding="utf-8")

    for dependency in payload["dependencies"]:
        assert dependency["url"] in script
        assert dependency["archive_name"] in script
        assert dependency["sha256"] in script
        assert dependency["install_subdir"] in script
