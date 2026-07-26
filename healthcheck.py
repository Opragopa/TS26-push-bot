#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Container health check: verifies that the deployed bot code can start."""

import json
import sys

import tg_sheet_monitor


print(json.dumps({
    "ok": True,
    "version": tg_sheet_monitor.APP_VERSION,
    "python": sys.version.split()[0],
}, ensure_ascii=False))
