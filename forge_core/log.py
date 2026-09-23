"""Shared logging + CI glue (GITHUB_OUTPUT / STEP_SUMMARY aware)."""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

_T0 = time.time()


def _ts() -> str:
    return time.strftime("%H:%M:%S", time.gmtime())


def log(msg: str) -> None:
    print(f"\033[1;34m[{_ts()}]\033[0m {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"\033[1;32m[{_ts()} OK\033[0m {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"\033[1;33m[{_ts()} WARN\033[0m {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> "None":
    print(f"\033[1;31m[{_ts()} FAIL\033[0m {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def out(name: str, value: str) -> None:
    """Write a GitHub Actions output (no-op outside CI)."""
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    print(f"OUT: {name}={value}", flush=True)


def summary(line: str) -> None:
    """Append a markdown line to the GitHub Actions step summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def notice(text: str, title: str = "ROMForge") -> None:
    """GitHub annotation (throttled by callers; cap is 10/step, 50/job)."""
    safe = text.replace("%", "%25").replace("\r", "").replace("\n", " ")
    print(f"::notice title={title}::{safe}", flush=True)


def elapsed_min() -> float:
    return (time.time() - _T0) / 60.0
