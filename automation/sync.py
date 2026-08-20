#!/usr/bin/env python3
"""Daily TikTok analytics upsert for the Scoban Feishu Base."""

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import certifi
from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.enc"
TLS = ssl.create_default_context(cafile=certifi.where())


def request(url, data=None, headers=None, form=False):
    headers = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=60, context=TLS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Remote API request failed with HTTP {error.code}") from error


def load_state(cipher):
    try:
        return json.loads(cipher.decrypt(STATE_FILE.read_bytes()))
    except (InvalidToken, ValueError, KeyError) as error:
        raise RuntimeError("Encrypted sync state could not be opened") from error


def save_state(cipher, state):
    temporary = STATE_FILE.with_suffix(".enc.tmp")
    temporary.write_bytes(cipher.encrypt(json.dumps(state, ensure_ascii=False).encode()))
    temporary.replace(STATE_FILE)


def refresh_tiktok(state, account):
    result = request(
        "https://open.tiktokapis.com/v2/oauth/token/",
        {
            "client_key": state["tiktok_client_key"],
            "client_secret": state["tiktok_client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": account["tokens"]["refresh_token"],
        },
        form=True,
    )
    if not result.get("access_token") or not result.get("refresh_token"):
        raise RuntimeError("TikTok token renewal failed")
    account["tokens"] = result
    return result["access_token"]


def fetch_videos(access_token):
    fields = "id,title,video_description,create_time,share_url,cover_image_url,view_count,like_count,comment_count,share_count"
    url = "https://open.tiktokapis.com/v2/video/list/?fields=" + fields
    videos, cursor = [], 0
    while True:
        result = request(
            url,
            {"max_count": 20, "cursor": cursor},
            {"Authorization": "Bearer " + access_token},
        )
        error = result.get("error") or {}
        if error.get("code") not in (None, 0, "ok"):
            raise RuntimeError(f"TikTok video listing failed: {error.get('code')}")
        data = result.get("data") or {}
        batch = data.get("videos") or []
        videos.extend(batch)
        if not data.get("has_more") or not batch:
            return videos
        cursor = data.get("cursor")


def feishu_token(state):
    result = request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": state["feishu_app_id"], "app_secret": state["feishu_app_secret"]},
    )
    if result.get("code") != 0:
        raise RuntimeError("Feishu authentication failed")
    return result["tenant_access_token"]


def feishu(path, token, data=None):
    result = request(
        "https://open.feishu.cn/open-apis" + path,
        data,
        {"Authorization": "Bearer " + token},
    )
    if result.get("code") != 0:
        raise RuntimeError(f"Feishu request failed: {result.get('code')} {result.get('msg')}")
    return result.get("data") or {}


def existing_records(state, token, table_id):
    records, page_token = [], None
    while True:
        query = {"page_size": 500}
        if page_token:
            query["page_token"] = page_token
        path = (
            f"/bitable/v1/apps/{state['feishu_app_token']}/tables/"
            f"{table_id}/records?{urllib.parse.urlencode(query)}"
        )
        data = feishu(path, token)
        records.extend(data.get("items") or [])
        if not data.get("has_more"):
            return records
        page_token = data.get("page_token")


def chunks(items, size=100):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def upload_cover(cover_url, video_id, app_token, tenant_token):
    try:
        download_request = urllib.request.Request(
            cover_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(download_request, timeout=45, context=TLS) as response:
            image = response.read()
            mime = response.headers.get_content_type()
        boundary = "----Scoban" + uuid.uuid4().hex
        extension = {"image/webp": "webp", "image/png": "png"}.get(mime, "jpg")
        filename = f"{video_id}.{extension}"
        parts = []

        def field(name, value):
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    str(value).encode(),
                    b"\r\n",
                ]
            )

        field("file_name", filename)
        field("parent_type", "bitable_image")
        field("parent_node", app_token)
        field("size", len(image))
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {mime}\r\n\r\n".encode(),
                image,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        upload_request = urllib.request.Request(
            "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
            data=b"".join(parts),
            headers={
                "Authorization": "Bearer " + tenant_token,
                "Content-Type": "multipart/form-data; boundary=" + boundary,
            },
        )
        with urllib.request.urlopen(upload_request, timeout=60, context=TLS) as response:
            result = json.loads(response.read())
        file_token = (result.get("data") or {}).get("file_token")
        if result.get("code") != 0 or not file_token:
            return None
        return [{"file_token": file_token}]
    except Exception:
        return None


def main():
    key = os.environ.get("SYNC_ENCRYPTION_KEY", "").encode()
    if not key:
        raise RuntimeError("SYNC_ENCRYPTION_KEY is not configured")
    cipher = Fernet(key)
    state = load_state(cipher)
    token = feishu_token(state)
    now = int(time.time() * 1000)
    total_existing = total_creates = total_updates = 0
    covers_added = cover_failures = 0
    account_counts = []
    for account in state["accounts"]:
        account_name = account["name"]
        table_id = account["table_id"]
        access_token = refresh_tiktok(state, account)
        videos = fetch_videos(access_token)
        account_counts.append(f"{account_name}={len(videos)}")
        existing = existing_records(state, token, table_id)
        total_existing += len(existing)
        records_by_video = {
            str(record.get("fields", {}).get("Video ID") or ""): record
            for record in existing
            if record.get("fields", {}).get("Video ID")
        }
        creates, updates = [], []
        for video in videos:
            video_id = str(video.get("id", ""))
            share_url = video.get("share_url") or ""
            fields = {
                "Video ID": video_id,
                "Caption": video.get("title") or video.get("video_description") or "",
                "Posted Date": int(video.get("create_time") or 0) * 1000,
                "Views": int(video.get("view_count") or 0),
                "Likes": int(video.get("like_count") or 0),
                "Comments": int(video.get("comment_count") or 0),
                "Shares": int(video.get("share_count") or 0),
                "Last Updated": now,
            }
            if share_url:
                fields["TikTok URL"] = {"link": share_url, "text": share_url}
            existing_record = records_by_video.get(video_id)
            has_cover = bool((existing_record or {}).get("fields", {}).get("Video Cover"))
            if not has_cover and video.get("cover_image_url"):
                cover = upload_cover(
                    video["cover_image_url"],
                    video_id,
                    state["feishu_app_token"],
                    token,
                )
                if cover:
                    fields["Video Cover"] = cover
                    covers_added += 1
                else:
                    cover_failures += 1
            if existing_record:
                updates.append({"record_id": existing_record["record_id"], "fields": fields})
            else:
                creates.append({"fields": fields})
        base = (
            f"/bitable/v1/apps/{state['feishu_app_token']}/tables/"
            f"{table_id}/records"
        )
        for batch in chunks(updates):
            feishu(base + "/batch_update", token, {"records": batch})
        for batch in chunks(creates):
            feishu(base + "/batch_create", token, {"records": batch})
        total_updates += len(updates)
        total_creates += len(creates)
    save_state(cipher, state)
    print(
        f"Sync complete: {total_updates} updated, {total_creates} added, "
        f"{total_existing + total_creates} total rows across separate tables "
        f"({', '.join(account_counts)}); {covers_added} covers added, "
        f"{cover_failures} covers unavailable."
    )


if __name__ == "__main__":
    main()
