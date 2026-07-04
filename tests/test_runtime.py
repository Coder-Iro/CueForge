from pathlib import Path

from cueforge import runtime


def test_find_executable_prefers_explicit_path(tmp_path: Path) -> None:
    explicit = tmp_path / "tools" / "ffmpeg.exe"
    explicit.parent.mkdir()
    explicit.write_text("", encoding="utf-8")

    status = runtime.find_executable("ffmpeg", explicit_path=explicit, root=tmp_path)

    assert status.path == explicit
    assert status.source == "settings"


def test_find_executable_falls_back_to_bundled_bin(tmp_path: Path, monkeypatch) -> None:
    bundled = tmp_path / "bin" / "deno" / "deno.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("", encoding="utf-8")
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)

    status = runtime.find_executable("deno", root=tmp_path)

    assert status.path == bundled
    assert status.source == "bundled"


def test_find_executable_falls_back_to_winget_packages(tmp_path: Path, monkeypatch) -> None:
    deno = (
        tmp_path
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "deno-x86_64-pc-windows-msvc"
        / "deno.exe"
    )
    deno.parent.mkdir(parents=True)
    deno.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    monkeypatch.setattr(runtime.os, "name", "nt")

    status = runtime.find_executable("deno", root=tmp_path)

    assert status.path == deno
    assert status.source == "winget"


def test_configure_dependency_path_prepends_bundled_dirs(tmp_path: Path, monkeypatch) -> None:
    nested = tmp_path / "bin" / "deno"
    nested.mkdir(parents=True)
    monkeypatch.setenv("PATH", "C:\\Windows")

    added = runtime.configure_dependency_path(root=tmp_path)

    assert tmp_path / "bin" in added
    assert nested in added
    assert str(tmp_path / "bin") in runtime.os.environ["PATH"]


def test_format_diagnostics_includes_dependencies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_with_version", lambda status, args: status)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-local-app-data"))

    output = runtime.format_diagnostics(root=tmp_path)

    assert "python:" in output
    assert "ffmpeg: missing: <missing>" in output
    assert "deno: missing: <missing>" in output
