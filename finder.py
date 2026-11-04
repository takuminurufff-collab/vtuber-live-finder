#!/usr/bin/env python3
"""
うぶごえライブ：データ更新スクリプト

1. 発見  … 配信中の動画をキーワード検索し、小規模なVTuberチャンネルを data/channels.json に蓄積
2. 更新  … 蓄積したチャンネルの登録者数と、動画・ショート・ライブの本数を更新
3. 確認  … 各チャンネルのRSS（API枠を使わない）から最新動画を取り、いま配信中かを判定
4. 出力  … ページ用の site/data.json を書き出す（絞り込みはページ側で行う）

手元で試すとき:
  pip install requests
  (PowerShell) $env:YOUTUBE_API_KEY="キー"
  python finder.py
"""

import html
import json
import os
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

# ===== 設定 =====
# 蓄積するチャンネルの登録者数上限（ページ側ではこれ以下の数字で絞り込める）
POOL_MAX_SUBSCRIBERS = int(os.environ.get("POOL_MAX_SUBSCRIBERS", 10000))
# 検索の計画。1回の実行で QUERIES_PER_RUN 個ずつ順番に使う（1回100ユニット）
#   live     … いま配信中の動画
#   upcoming … 配信予定（予告枠）。初配信の待機所を拾う
#   recent   … 直近24時間に投稿された動画（新着順）。配信していない新人も拾う
SEARCH_PLAN = [
    ("live", "VTuber"), ("recent", "#新人Vtuber"),
    ("live", "新人VTuber"), ("upcoming", "初配信"),
    ("live", "初配信"), ("recent", "初配信 Vtuber"),
    ("live", "個人勢VTuber"), ("recent", "#Vtuber準備中"),
    ("live", "Vtuber 雑談"), ("upcoming", "新人Vtuber"),
    ("live", "Vtuber 歌枠"), ("recent", "Vtuber 自己紹介"),
    ("live", "Vtuber ゲーム実況"), ("recent", "#個人勢Vtuber"),
    ("live", "Vライバー"), ("upcoming", "Vtuber デビュー"),
    ("live", "Vtuber 作業"), ("recent", "Vtuber デビュー"),
    ("live", "Vtuber 朝活"), ("recent", "#新人Vtuberを発掘せよ"),
]
QUERIES_PER_RUN = int(os.environ.get("QUERIES_PER_RUN", 1))
# 本数（動画・ショート・ライブ）を数え直す間隔と、1回あたりの最大チャンネル数（1件3ユニット）
COUNTS_REFRESH_HOURS = 24
COUNTS_PER_RUN = 60
# RSSで確認する最新動画の数
RSS_LATEST = 3
REGION_CODE = "JP"
RELEVANCE_LANGUAGE = "ja"
REQUIRED_KEYWORDS = ["vtuber", "ブイチューバー", "初配信", "新人", "個人勢", "vライバー", "バーチャル"]

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data", "channels.json")
OUT_PATH = os.path.join(ROOT, "site", "data.json")
# =================

API = "https://www.googleapis.com/youtube/v3"
API_KEY = None
quota_used = 0
NOW = datetime.now(timezone.utc)
session = requests.Session()
session.headers["User-Agent"] = "ubugoe-live/1.0"


def iso(dt):
    return dt.isoformat(timespec="seconds")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def api_get(endpoint, params, cost):
    global quota_used
    r = session.get(f"{API}/{endpoint}", params=dict(params, key=API_KEY), timeout=20)
    quota_used += cost
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        try:
            reason = r.json()["error"]["errors"][0]["reason"]
        except Exception:
            reason = r.text[:200]
        if reason == "quotaExceeded":
            raise QuotaExceeded()
        if reason == "playlistNotFound":
            return None
        raise RuntimeError(f"{endpoint} でエラー ({r.status_code}): {reason}")
    return r.json()


class QuotaExceeded(Exception):
    pass


def chunks(items, n=50):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


def thumb(thumbs):
    for size in ("medium", "high", "default"):
        if size in (thumbs or {}):
            return thumbs[size]["url"]
    return None


def matches_keywords(*texts):
    joined = " ".join(t or "" for t in texts).lower()
    return any(k.lower() in joined for k in REQUIRED_KEYWORDS)


# ---------- 1. 発見 ----------
def pick_queries():
    start = (int(NOW.timestamp()) // 3600 * QUERIES_PER_RUN) % len(SEARCH_PLAN)
    return [SEARCH_PLAN[(start + i) % len(SEARCH_PLAN)] for i in range(QUERIES_PER_RUN)]


def discover(queries):
    """検索して {channel_id: snippet} を返す"""
    found = {}
    for mode, q in queries:
        params = {"part": "snippet", "type": "video", "q": q, "maxResults": 50,
                  "regionCode": REGION_CODE, "relevanceLanguage": RELEVANCE_LANGUAGE}
        if mode in ("live", "upcoming"):
            params["eventType"] = mode
        else:
            params["order"] = "date"
            params["publishedAfter"] = iso(NOW - timedelta(hours=24)).replace("+00:00", "Z")
        data = api_get("search", params, cost=100)
        for item in (data or {}).get("items", []):
            sn = dict(item["snippet"], video_id=item["id"]["videoId"])
            found.setdefault(sn["channelId"], sn)
    return found


# ---------- 2. 更新 ----------
def fetch_channels(ids):
    out = {}
    for batch in chunks(ids):
        data = api_get("channels", {"part": "snippet,statistics", "id": ",".join(batch),
                                    "maxResults": 50}, cost=1)
        for ch in (data or {}).get("items", []):
            out[ch["id"]] = ch
    return out


def playlist_total(playlist_id):
    data = api_get("playlistItems", {"part": "id", "playlistId": playlist_id, "maxResults": 1}, cost=1)
    return 0 if data is None else data["pageInfo"]["totalResults"]


def refresh_counts(ch_id, rec):
    suffix = ch_id[2:]
    rec["videos"] = playlist_total("UULF" + suffix)   # 通常の動画
    rec["shorts"] = playlist_total("UUSH" + suffix)   # ショート
    rec["lives"] = playlist_total("UULV" + suffix)    # ライブ
    rec["counts_at"] = iso(NOW)


# ---------- 3. 確認 ----------
def rss_latest(ch_id):
    try:
        r = session.get("https://www.youtube.com/feeds/videos.xml",
                        params={"channel_id": ch_id}, timeout=15)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        ids = [e.findtext("yt:videoId", namespaces=ns) for e in root.findall("a:entry", ns)]
        return [i for i in ids if i][:RSS_LATEST]
    except Exception:
        return []


def fetch_videos(ids):
    out = {}
    for batch in chunks(ids):
        data = api_get("videos", {"part": "snippet,liveStreamingDetails", "id": ",".join(batch)}, cost=1)
        for v in (data or {}).get("items", []):
            out[v["id"]] = v
    return out


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            db = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        db = {}

    queries = pick_queries()
    discovered = {}
    try:
        discovered = discover(queries)

        # 新しく見つかったチャンネルを登録（登録者数・キーワードで選別）
        new_ids = [c for c in discovered if c not in db]
        for cid, ch in fetch_channels(new_ids).items():
            st, sn = ch["statistics"], ch["snippet"]
            if st.get("hiddenSubscriberCount") or "subscriberCount" not in st:
                continue
            if int(st["subscriberCount"]) > POOL_MAX_SUBSCRIBERS:
                continue
            live_sn = discovered[cid]
            if not matches_keywords(live_sn["title"], sn["title"], sn.get("description")):
                continue
            db[cid] = {"first_seen": iso(NOW), "counts_at": None}

        # 全チャンネルの登録者数・名前・アイコンを更新。上限を超えたら外す
        for cid, ch in fetch_channels(list(db)).items():
            st, sn = ch["statistics"], ch["snippet"]
            subs = int(st.get("subscriberCount", 0))
            if st.get("hiddenSubscriberCount") or subs > POOL_MAX_SUBSCRIBERS:
                db.pop(cid, None)
                continue
            db[cid].update(name=sn["title"], icon=thumb(sn.get("thumbnails")), subscribers=subs)
        for cid in [c for c, r in db.items() if "name" not in r]:
            db.pop(cid)  # 削除・非公開になったチャンネル

        # RSSで最新動画を集めて、配信中かどうかを判定
        with ThreadPoolExecutor(max_workers=12) as ex:
            latest = dict(zip(db, ex.map(rss_latest, list(db))))
        video_ids = set()
        for cid, ids in latest.items():
            video_ids.update(ids)
        for cid, sn in discovered.items():
            if cid in db:
                video_ids.add(sn["video_id"])
        videos = fetch_videos(video_ids)

        by_channel = {}
        for v in videos.values():
            by_channel.setdefault(v["snippet"]["channelId"], []).append(v)

        for cid, rec in db.items():
            vs = by_channel.get(cid, [])
            live = next((v for v in vs if v["snippet"].get("liveBroadcastContent") == "live"), None)
            was_live = rec.get("is_live")
            rec["is_live"] = live is not None
            if live:
                d = live.get("liveStreamingDetails", {})
                rec["live"] = {
                    "video_id": live["id"],
                    "title": live["snippet"]["title"],
                    "thumbnail": thumb(live["snippet"].get("thumbnails")),
                    "viewers": int(d["concurrentViewers"]) if "concurrentViewers" in d else None,
                    "started_at": d.get("actualStartTime"),
                }
                rec["last_live_at"] = iso(NOW)
                if not was_live:
                    rec["counts_at"] = None  # 配信が始まったらライブ本数を数え直す
            else:
                rec.pop("live", None)
                ended = [parse_iso(v["liveStreamingDetails"].get("actualEndTime"))
                         for v in vs if v.get("liveStreamingDetails", {}).get("actualEndTime")]
                if ended:
                    newest = max(ended)
                    prev = parse_iso(rec.get("last_live_at"))
                    if not prev or newest > prev:
                        rec["last_live_at"] = iso(newest)
            if vs:
                newest_v = max(vs, key=lambda v: v["snippet"]["publishedAt"])
                rec["latest"] = {"video_id": newest_v["id"], "title": newest_v["snippet"]["title"],
                                 "thumbnail": thumb(newest_v["snippet"].get("thumbnails"))}

        # 本数の更新（未取得 → 配信中 → 古い順に、上限まで）
        def stale(rec):
            t = parse_iso(rec.get("counts_at"))
            return t is None or NOW - t > timedelta(hours=COUNTS_REFRESH_HOURS)
        queue = sorted((c for c, r in db.items() if stale(r)),
                       key=lambda c: (db[c].get("counts_at") is not None,
                                      not db[c].get("is_live"),
                                      db[c].get("counts_at") or ""))
        for cid in queue[:COUNTS_PER_RUN]:
            refresh_counts(cid, db[cid])

    except QuotaExceeded:
        print("APIの1日の上限に達したため、ここまでの結果で保存します。")

    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=0, sort_keys=True)

    channels = []
    for cid, r in db.items():
        if "name" not in r:
            continue
        item = {"id": cid, "name": html.unescape(r["name"]), "icon": r.get("icon"),
                "subscribers": r.get("subscribers"), "videos": r.get("videos"),
                "shorts": r.get("shorts"), "lives": r.get("lives"),
                "is_live": bool(r.get("is_live")), "last_live_at": r.get("last_live_at"),
                "first_seen": r.get("first_seen")}
        if r.get("live"):
            item["live"] = dict(r["live"], title=html.unescape(r["live"]["title"]))
        if r.get("latest"):
            item["latest"] = dict(r["latest"], title=html.unescape(r["latest"]["title"]))
        channels.append(item)

    out = {"updated_at": iso(datetime.now(timezone.utc)), "pool_max_subscribers": POOL_MAX_SUBSCRIBERS,
           "queries": [f"{m}:{q}" for m, q in queries], "quota_used": quota_used, "channels": channels}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    live_n = sum(1 for c in channels if c["is_live"])
    print(f"検索語 {queries} / 蓄積 {len(channels)} チャンネル（うち配信中 {live_n}）/ API消費 約{quota_used}ユニット")


if __name__ == "__main__":
    API_KEY = os.environ.get("YOUTUBE_API_KEY")
    if not API_KEY:
        sys.exit("環境変数 YOUTUBE_API_KEY を設定してください")
    main()
