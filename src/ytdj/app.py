"""Application entry point."""

from __future__ import annotations

import sys

from ytdj.runtime import configure_dependency_path, format_diagnostics


def main() -> int:
    configure_dependency_path()
    if "--diagnose" in sys.argv:
        print(format_diagnostics())
        return 0
    from ytdj.gui.main_window import run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
