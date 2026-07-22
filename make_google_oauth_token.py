#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create GOOGLE_OAUTH_USER_JSON for editing Google Sheets as a user."""

import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def find_client_secret_path(argv):
    if argv:
        return Path(argv[0]).expanduser()
    env_path = os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    candidates = [Path("oauth_client.json")]
    candidates.extend(sorted(Path(".").glob("client_secret*.json")))
    for path in candidates:
        if path.exists():
            return path
    raise SystemExit("Не найден OAuth Client JSON. Передайте путь: python3 make_google_oauth_token.py client_secret_....json")


def main(argv=None):
    client_secret_path = find_client_secret_path(list(argv or []))
    print("Использую OAuth Client JSON: {}".format(client_secret_path))
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit("Не установлен google-auth-oauthlib. Выполните: python3 -m pip install -r requirements.txt") from exc
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    credentials = flow.run_local_server(port=0, prompt="consent")
    data = {
        "type": "authorized_user",
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "refresh_token": credentials.refresh_token,
    }
    print("\nСкопируйте это в Bothost как GOOGLE_OAUTH_USER_JSON:\n")
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
