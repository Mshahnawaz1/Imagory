"""Entry point: ``python -m imagory``."""

import multiprocessing


def main() -> int:
    from imagory.app import run

    return run()


if __name__ == "__main__":
    # Must run before anything else, or worker processes fail in a PyInstaller build.
    multiprocessing.freeze_support()
    raise SystemExit(main())
