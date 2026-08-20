#!/usr/bin/env python3
"""Rebuild the combined Feishu Sheet tab from the two TikTok Base tables."""

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import certifi
from cryptography.fernet import Fernet, InvalidToken


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.enc"
TLS = ssl.create_default_context(cafile=certifi.where())
HEADERS = [
    "Posted Date",
    "Account",
    "Video ID",
    "Cover Image",
    "TikTok URL",
    "Views",
    "Caption",
]


def http(method, url, data=None, headers=None):
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode()
    request_headers = dict(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=TLS) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"Feishu HTTP {error.code}: {detail}") from error
    return json.loads(raw) if raw else {}


def load_state():
    key = os.environ.get("SYNC_ENCRYPTION_KEY", "").encode()
    if not key:
        raise RuntimeError("SYNC_ENCRYPTION_KEY is not configured")
    try:
        return json.loads(Fernet(key).decrypt(STATE_FILE.read_bytes()))
    except (InvalidToken, ValueError, KeyError) as error:
        raise RuntimeError("Encrypted sync state could not be opened") from error


def save_state(state):
    key = os.environ["SYNC_ENCRYPTION_KEY"].encode()
    temporary = STATE_FILE.with_suffix(".enc.tmp")
    temporary.write_bytes(Fernet(key).encrypt(json.dumps(state, ensure_ascii=False).encode()))
    temporary.replace(STATE_FILE)


def tenant_token(state):
    result = http(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": state["feishu_app_id"], "app_secret": state["feishu_app_secret"]},
    )
    if result.get("code") != 0:
        raise RuntimeError(f"Feishu authentication failed: {result.get('msg')}")
    return result["tenant_access_token"]


def feishu(method, path, token, data=None):
    result = http(
        method,
        "https://open.feishu.cn/open-apis" + path,
        data,
        {"Authorization": "Bearer " + token},
    )
    if result.get("code", 0) != 0:
        raise RuntimeError(f"Feishu request failed: {result.get('code')} {result.get('msg')}")
    return result.get("data") or {}


def download_media(file_token, token):
    request = urllib.request.Request(
        f"https://open.feishu.cn/open-apis/drive/v1/medias/{file_token}/download",
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=TLS) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"Feishu media HTTP {error.code}: {detail}") from error


def records(app_token, table_id, token):
    items, page_token = [], None
    while True:
        query = {"page_size": 500}
        if page_token:
            query["page_token"] = page_token
        data = feishu(
            "GET",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records?"
            + urllib.parse.urlencode(query),
            token,
        )
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            return items
        page_token = data.get("page_token")


def scalar(value):
    if value is None:
        return ""
    if isinstance(value, list):
        if all(isinstance(item, dict) for item in value):
            return ", ".join(
                str(item.get("text") or item.get("name") or item.get("link") or "")
                for item in value
            )
        return ", ".join(map(str, value))
    if isinstance(value, dict):
        return value.get("text") or value.get("name") or value.get("link") or ""
    return value


def date_value(value):
    value = scalar(value)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y/%m/%d %H:%M")
    return value


def account_rows(account):
    result = []
    for record in account["records"]:
        fields = record.get("fields") or {}
        covers = fields.get("Video Cover") or []
        cover = covers[0] if covers and isinstance(covers[0], dict) else {}
        result.append(
            [
                date_value(fields.get("Posted Date")),
                account["name"],
                str(scalar(fields.get("Video ID"))),
                "",
                scalar(fields.get("TikTok URL")),
                scalar(fields.get("Views")),
                scalar(fields.get("Caption")),
                cover.get("file_token") or "",
                cover.get("name") or "cover.jpg",
            ]
        )
    return result


def destination(state, token):
    spreadsheet_token = state.get("combined_sheet_token")
    sheet_id = state.get("combined_sheet_id")
    if spreadsheet_token and sheet_id:
        return spreadsheet_token, sheet_id

    created = feishu(
        "POST",
        "/sheets/v3/spreadsheets",
        token,
        {"title": "Scoban + Saludent TikTok Combined Data"},
    )
    spreadsheet = created.get("spreadsheet") or created
    spreadsheet_token = spreadsheet.get("spreadsheet_token")
    if not spreadsheet_token:
        raise RuntimeError("Feishu did not return the new spreadsheet token")
    sheets = feishu(
        "GET",
        f"/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        token,
    ).get("sheets") or []
    if not sheets:
        raise RuntimeError("The new spreadsheet has no worksheet")
    sheet_id = sheets[0]["sheet_id"]
    state["combined_sheet_token"] = spreadsheet_token
    state["combined_sheet_id"] = sheet_id
    save_state(state)
    feishu(
        "PATCH",
        f"/drive/v1/permissions/{spreadsheet_token}/public?type=sheet",
        token,
        {"link_share_entity": "tenant_editable"},
    )
    return spreadsheet_token, sheet_id


def rebuild_sheet(spreadsheet_token, sheet_id, rows, token):
    all_rows = [HEADERS] + [row[:7] for row in rows]
    for start in range(0, len(all_rows), 500):
        chunk = all_rows[start : start + 500]
        first = start + 1
        last = start + len(chunk)
        feishu(
            "PUT",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
            token,
            {
                "valueRange": {
                    "range": f"{sheet_id}!A{first}:G{last}",
                    "values": chunk,
                }
            },
        )
    for start in range(len(all_rows), 20_000, 500):
        size = min(500, 20_000 - start)
        feishu(
            "PUT",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/values",
            token,
            {
                "valueRange": {
                    "range": f"{sheet_id}!A{start + 1}:G{start + size}",
                    "values": [[""] * 7 for _ in range(size)],
                }
            },
        )
    covers_written = 0
    for sheet_row, row in enumerate(rows, start=2):
        if not row[7]:
            continue
        image = download_media(row[7], token)
        feishu(
            "POST",
            f"/sheets/v2/spreadsheets/{spreadsheet_token}/values_image",
            token,
            {
                "range": f"{sheet_id}!D{sheet_row}:D{sheet_row}",
                "image": list(image),
                "name": row[8],
            },
        )
        covers_written += 1
    return covers_written


def main():
    state = load_state()
    token = tenant_token(state)
    combined = []
    counts = {}
    for configured in state["accounts"]:
        current = records(state["feishu_app_token"], configured["table_id"], token)
        account = {"name": configured["name"], "records": current}
        rows = account_rows(account)
        combined.extend(rows)
        counts[configured["name"]] = len(rows)
    combined.sort(key=lambda row: str(row[0]), reverse=True)
    spreadsheet_token, sheet_id = destination(state, token)
    covers_written = rebuild_sheet(spreadsheet_token, sheet_id, combined, token)
    print(
        f"Combined Sheet refreshed: {len(combined)} rows ({counts}), "
        f"{covers_written} covers; "
        f"https://pqwikxlxg3.feishu.cn/sheets/{spreadsheet_token}?sheet={sheet_id}"
    )


if __name__ == "__main__":
    main()
