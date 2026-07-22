#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create GOOGLE_OAUTH_USER_JSON for editing Google Sheets as a user."""

import json

from google_auth_oauthlib.flow import InstalledAppFlow


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def main():
    print("Скачайте OAuth Client JSON из Google Cloud и сохраните рядом как oauth_client.json.")
    flow = InstalledAppFlow.from_client_secrets_file("oauth_client.json", SCOPES)
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
    main()
