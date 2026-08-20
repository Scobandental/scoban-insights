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
SHEET_WIKI_TOKEN = "Q4fow4YHVi8fkwkdJFDcb75InRg"
SHEET_ID = "dSOaUs"
HEADERS = ["Account", "Video ID", "Caption", "Posted Date", "TikTok URL", "Views"]


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
        result.append(
            [
                account["name"],
                str(scalar(fields.get("Video ID"))),
                scalar(fields.get("Caption")),
                date_value(fields.get("Posted Date")),
                scalar(fields.get("TikTok URL")),
                scalar(fields.get("Views")),
            ]
        )
    return result


def sheet_token(token):
    query = urllib.parse.urlencode({"token": SHEET_WIKI_TOKEN})
    data = feishu("GET", f"/wiki/v2/spaces/get_node?{query}", token)
    node = data.get("node") or {}
    if node.get("obj_type") != "sheet":
        raise RuntimeError(f"Destination wiki node is {node.get('obj_type')}, expected sheet")
    return node["obj_token"]


def rebuild_sheet(spreadsheet_token, rows, token):
    clear_range = urllib.parse.quote(f"{SHEET_ID}!A1:F20000", safe="")
    feishu(
        "DELETE",
        f"/sheets/v2/spreadsheets/{spreadsheet_token}/values/{clear_range}",
        token,
    )
    all_rows = [HEADERS] + rows
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
                    "range": f"{SHEET_ID}!A{first}:F{last}",
                    "values": chunk,
                }
            },
        )


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
    combined.sort(key=lambda row: str(row[3]), reverse=True)
    rebuild_sheet(sheet_token(token), combined, token)
    print(f"Combined Sheet refreshed: {len(combined)} rows ({counts})")


if __name__ == "__main__":
    main()
