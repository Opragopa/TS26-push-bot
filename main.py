#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bothost entrypoint."""

import runtime_bootstrap

runtime_bootstrap.ensure_runtime_dependencies()

import tg_sheet_monitor


if __name__ == "__main__":
    tg_sheet_monitor.main()
