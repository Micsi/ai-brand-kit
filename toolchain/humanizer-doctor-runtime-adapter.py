#!/usr/bin/env python3
"""Run the pinned Humanizer doctor with a removable-volume-safe deadline."""

from __future__ import annotations

import importlib.util
import pathlib
import sys


CHILD_TIMEOUT_SECONDS = 600


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: humanizer-doctor-runtime-adapter.py DOCTOR")
    path = pathlib.Path(sys.argv[1]).resolve(strict=True)
    spec = importlib.util.spec_from_file_location("humanizer_doctor", path)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load Humanizer doctor")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.run_command

    def bounded(command: list[str], **kwargs: object):
        kwargs["timeout"] = max(int(kwargs.get("timeout", 20)), CHILD_TIMEOUT_SECONDS)
        return original(command, **kwargs)

    module.run_command = bounded
    return module.main(["--require-full"])


if __name__ == "__main__":
    raise SystemExit(main())
