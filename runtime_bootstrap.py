#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Best-effort dependency bootstrap for minimal bot hosts."""

import importlib
import os
import subprocess
import sys
from pathlib import Path


REQUIRED_RUNTIME_MODULES = (
    "gspread",
    "google.oauth2.credentials",
    "google.oauth2.service_account",
)


def missing_runtime_modules():
    missing = []
    for module_name in REQUIRED_RUNTIME_MODULES:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name and (module_name == exc.name or module_name.startswith(exc.name + ".")):
                missing.append(module_name)
            else:
                raise
    return missing


def ensure_runtime_dependencies():
    missing = missing_runtime_modules()
    if not missing:
        return False
    if os.environ.get("TS26_SKIP_DEPENDENCY_BOOTSTRAP", "").strip().lower() in {"1", "true", "yes", "on"}:
        raise ModuleNotFoundError("Missing runtime dependencies: {}".format(", ".join(missing)))
    requirements_path = Path(__file__).resolve().parent / "requirements.txt"
    if not requirements_path.exists():
        raise ModuleNotFoundError("Missing runtime dependencies and requirements.txt was not found: {}".format(", ".join(missing)))
    print(
        "[bootstrap] Missing Python dependencies: {}. Installing requirements.txt...".format(", ".join(missing)),
        flush=True,
    )
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", str(requirements_path)])
    still_missing = missing_runtime_modules()
    if still_missing:
        raise ModuleNotFoundError("Could not install runtime dependencies: {}".format(", ".join(still_missing)))
    print("[bootstrap] Python dependencies installed.", flush=True)
    return True
