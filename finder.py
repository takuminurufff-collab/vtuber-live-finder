#!/usr/bin/env python3
"""
新人VTuberライブ発掘：データ更新スクリプト

条件:
  1. 現在YouTubeでライブ配信中
  2. チャンネル登録者数が MAX_SUBSCRIBERS 人以内
  3. チャンネルの「ライブ」タブにある動画が MAX_LIVE_VIDEOS 本以内

結果を site/data.json に書き出します。GitHub Actions から定期実行される想定です。
手元で試すとき:
  pip install requests
  (PowerShell) $env:YOUTUBE_API_KEY="キー"   /  (Mac) export YOUTUBE_API_KEY="キー"
  python finder.py
"""

import html
import json
import os
import sys
from datetime import datetime, timezone

import requests

# ===== 設定 =====
MAX_SUBSCRIBERS = int(os.environ.get("MAX_SUBSCRIBERS", 1000))
MAX_LIVE_VIDEOS = int(os.environ.get("MAX_LIVE_VIDEOS", 5))
SEARCH_QUERIES = ["VTuber", "新人VTuber", "初配信"]   # 1語につき100ユニット
PAGES_PER_QUERY = 1                                   # 50件/ページ
REGION_CODE = "JP"
RELEVANCE_LANGUAGE = "ja"
REQUIRED_KEYWORDS = ["vtuber", "ブイチューバー", "初配信", "新人", "個人勢", "vライバー"]
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "data.json")
# =================

API = "https://www.googleapis.com/youtube/v3"
API_KEY = None
quota_used = 0


def api_get(endpoint, params, cost):
    global quota_used
    r = requests.get(f"{API}/{endpoint}", params=dict(params, key=API_KEY), timeout=20)
    quota_used += cost
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        try:
            reason = r.json()["error"]["errors"][0]["reason"]
        except Exception:
            reason = r.text[:200]
        if reason == "quotaExceeded":
            sys.exit("APIの1日の利用上限に達しました。日本時間17時以降に回復します。")
        raise RuntimeError(f"{endpoint} でエラー ({r.status_code}): {reason}")
    return r.json()


def chunks(items, n=50):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def search_live_videos():
    found = {}
    for q in SEARCH_QUERIES:
        token = None
        for _ in range(PAGES_PER_QUERY):
            params = {"part": "snippet", "type": "video", "eventType": "live", "q": q,
                      "maxResults": 50, "regionCode": REGION_CODE,
                      "relevanceLanguage": RELEVANCE_LANGUAGE}
            if token:
                params["pageToken"] = token
            data = api_get("search", params, cost=100)
            for item in data.get("items", []):
                found.setdefault(item["id"]["videoId"], item["snippet"])
            token = data.get("nextPageToken")
            if not token:
                break
    return found


def get_channels(channel_ids):
    result = {}
    for batch in chunks(channel_ids):
        data = api_get("channels", {"part": "snippet,statistics,contentDetails",
                                    "id": ",".join(batch), "maxResults": 50}, cost=1)
        for ch in data.get("items", []):
            result[ch["id"]] = ch
    return result


def count_live_tab(channel_id, uploads_playlist):
    """「ライブ」タブの本数。UC→UULV の再生リストで数え、無ければアップロード一覧から数える"""
    data = api_get("playlistItems", {"part": "id", "playlistId": "UULV" + channel_id[2:],
                                     "maxResults": 1}, cost=1)
    if data is not None:
        return data["pageInfo"]["totalResults"]
    count, token = 0, None
    for _ in range(4):
        params = {"part": "contentDetails", "playlistId": uploads_playlist, "maxResults": 50}
        if token:
            params["pageToken"] = token
        page = api_get("playlistItems", params, cost=1)
        if page is None:
            break
        ids = [it["contentDetails"]["videoId"] for it in page.get("items", [])]
        if ids:
            vids = api_get("videos", {"part": "liveStreamingDetails", "id": ",".join(ids)}, cost=1)
            count += sum(1 for v in vids.get("items", []) if "liveStreamingDetails" in v)
        token = page.get("nextPageToken")
        if count > MAX_LIVE_VIDEOS or not token:
            break
    return count


def get_live_details(video_ids):
    out = {}
    for batch in chunks(video_ids):
        data = api_get("videos", {"part": "liveStreamingDetails", "id": ",".join(batch)}, cost=1)
        for v in data.get("items", []):
            d = v.get("liveStreamingDetails", {})
            out[v["id"]] = (int(d["concurrentViewers"]) if "concurrentViewers" in d else None,
                            d.get("actualStartTime"))
    return out


def matches_keywords(*texts):
    if not REQUIRED_KEYWORDS:
        return True
    joined = " ".join(t or "" for t in texts).lower()
    return any(k.lower() in joined for k in REQUIRED_KEYWORDS)


def thumb(thumbs):
    for size in ("medium", "high", "default"):
        if size in (thumbs or {}):
            return thumbs[size]["url"]
    return None


def main():
    videos = search_live_videos()
    by_channel = {}
    for vid, sn in videos.items():
        by_channel.setdefault(sn["channelId"], (vid, sn))
    channels = get_channels(list(by_channel))

    candidates = []
    for cid, (vid, sn) in by_channel.items():
        ch = channels.get(cid)
        if not ch:
            continue
        st = ch["statistics"]
        if st.get("hiddenSubscriberCount") or "subscriberCount" not in st:
            continue
        subs = int(st["subscriberCount"])
        if subs > MAX_SUBSCRIBERS:
            continue
        if not matches_keywords(sn["title"], sn["channelTitle"], ch["snippet"].get("description")):
            continue
        candidates.append((cid, vid, sn, ch, subs))

    streams = []
    for cid, vid, sn, ch, subs in candidates:
        n = count_live_tab(cid, ch["contentDetails"]["relatedPlaylists"]["uploads"])
        if n <= MAX_LIVE_VIDEOS:
            streams.append({
                "channel": html.unescape(sn["channelTitle"]),
                "channel_id": cid,
                "channel_icon": thumb(ch["snippet"].get("thumbnails")),
                "subscribers": subs,
                "live_count": n,
                "video_id": vid,
                "title": html.unescape(sn["title"]),
                "thumbnail": thumb(sn.get("thumbnails")),
            })

    details = get_live_details([s["video_id"] for s in streams])
    for s in streams:
        s["viewers"], s["started_at"] = details.get(s["video_id"], (None, None))
    streams.sort(key=lambda s: (s["live_count"], s["subscribers"]))

    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "conditions": {"max_subscribers": MAX_SUBSCRIBERS, "max_live_videos": MAX_LIVE_VIDEOS},
        "scanned": len(videos),
        "streams": streams,
    }
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"配信中 {len(videos)} 件を確認 → 該当 {len(streams)} 件。API消費 約{quota_used}ユニット")


if __name__ == "__main__":
    API_KEY = os.environ.get("YOUTUBE_API_KEY")
    if not API_KEY:
        sys.exit("環境変数 YOUTUBE_API_KEY を設定してください")
    main()
