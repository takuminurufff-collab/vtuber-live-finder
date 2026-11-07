#!/usr/bin/env python3
"""
うぶごえライブ：データ更新スクリプト

1. 総ざらい … 「#新人Vtuber」などのタグが付いた動画を、投稿時刻で1時間ずつ区切って
               全ページめくる。前回の続きから今まで、すき間なく確認する
2. 配信検索 … 配信中・配信予定の動画をキーワード検索（タグを付けていない人を拾う）
3. 更新     … 蓄積したチャンネルの登録者数と、動画・ショート・ライブの本数を更新
4. 確認     … 各チャンネルのRSS（API枠を使わない）で新しい動画を見つけ、配信中かを判定
5. 出力     … ページ用の site/data.json を書き出す（絞り込みはページ側で行う）

API の1日の枠（太平洋時間0時＝日本時間17時にリセット）を data/state.json で数えて、
次のリセットまで均等に使い切るように、1回あたりの使用量を自動で決めます。

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
from zoneinfo import ZoneInfo

import requests

# ===== 設定 =====
# 蓄積するチャンネルの登録者数上限（初期値は実質無制限。絞り込みはページ側で行う）
POOL_MAX_SUBSCRIBERS = int(os.environ.get("POOL_MAX_SUBSCRIBERS", 1000000000))
# 1日に使うAPIユニット（Googleの上限は10,000。手動実行の分を少し残す）
DAILY_BUDGET = int(os.environ.get("DAILY_BUDGET", 9300))

# 総ざらいするタグ（投稿時刻で区切って全件たどる）
SWEEP_TAGS = ["#新人Vtuber", "#Vtuber準備中", "#個人勢Vtuber", "#新人Vtuberを発掘せよ"]
SWEEP_MAX_SPAN = timedelta(hours=24)   # 1回の検索で扱う最大の期間
SWEEP_MIN_SPAN = timedelta(minutes=15) # これ以上は細かくしない
SWEEP_SAFE_TOTAL = 400                 # 区間の件数がこれを超えたら期間を半分にする
SWEEP_MAX_LAG = timedelta(days=3)      # これより古い未確認分は諦める
SWEEP_DELAY = timedelta(minutes=10)    # 投稿直後は検索に出ないことがあるので少し待つ
SWEEP_MAX_PAGES = 10                   # YouTube検索が返すのは1区切りあたり最大500件

# 配信中・配信予定のキーワード検索（毎回1つずつ順番に）
LIVE_PLAN = [
    ("live", "VTuber"), ("live", "新人VTuber"), ("upcoming", "初配信"),
    ("live", "個人勢VTuber"), ("live", "Vtuber 雑談"), ("upcoming", "新人Vtuber"),
    ("live", "Vtuber 歌枠"), ("live", "Vtuber ゲーム実況"), ("upcoming", "Vtuber デビュー"),
    ("live", "Vライバー"), ("live", "Vtuber 作業"), ("live", "初配信"),
]

# 本数（動画・ショート・ライブ）を数え直す間隔（1チャンネル3ユニット）
COUNTS_REFRESH = timedelta(days=3)
RSS_LATEST = 3
REGION_CODE = "JP"
RELEVANCE_LANGUAGE = "ja"
# VTuberかどうかの判定に使う言葉（動画タイトル・概要・チャンネル名・チャンネル概要のどこか）
VTUBER_WORDS = ["vtuber", "ブイチューバー", "vライバー", "バーチャル", "個人勢", "準備中"]

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data", "channels.json")
STATE_PATH = os.path.join(ROOT, "data", "state.json")
OUT_PATH = os.path.join(ROOT, "site", "data.json")
# =================

API = "https://www.googleapis.com/youtube/v3"
API_KEY = None
NOW = datetime.now(timezone.utc)
PT = ZoneInfo("America/Los_Angeles")
session = requests.Session()
session.headers["User-Agent"] = "ubugoe-live/1.0"


class QuotaExceeded(Exception):
    pass


class Budget:
    """今日（太平洋時間）の使用量と、この実行で使ってよい量を管理する"""

    def __init__(self, state):
        today = NOW.astimezone(PT).date().isoformat()
        q = state.setdefault("quota", {})
        if q.get("day") != today:
            q.update(day=today, used=0)
        self.q = q
        remaining = max(0, DAILY_BUDGET - q["used"])
        last = parse_iso(state.get("last_run")) or NOW - timedelta(hours=1)
        since = min(max((NOW - last).total_seconds() / 3600, 1), 24)
        reset = datetime.combine(NOW.astimezone(PT).date() + timedelta(days=1),
                                 datetime.min.time(), PT)
        left = max((reset - NOW).total_seconds() / 3600, 1)
        self.run_cap = int(remaining * min(1.0, since / left))
        self.run_used = 0

    def spend(self, n):
        self.run_used += n
        self.q["used"] += n

    def run_left(self):
        return self.run_cap - self.run_used

    def day_left(self):
        return DAILY_BUDGET - self.q["used"]


budget = None


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def api_get(endpoint, params, cost):
    if budget.day_left() < cost:
        raise QuotaExceeded()
    r = session.get(f"{API}/{endpoint}", params=dict(params, key=API_KEY), timeout=20)
    budget.spend(cost)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        try:
            reason = r.json()["error"]["errors"][0]["reason"]
        except Exception:
            reason = r.text[:200]
        if reason == "quotaExceeded":
            budget.q["used"] = DAILY_BUDGET
            raise QuotaExceeded()
        if reason == "playlistNotFound":
            return None
        raise RuntimeError(f"{endpoint} でエラー ({r.status_code}): {reason}")
    return r.json()


def chunks(items, n=50):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


def thumb(thumbs):
    for size in ("medium", "high", "default"):
        if size in (thumbs or {}):
            return thumbs[size]["url"]
    return None


def looks_vtuber(*texts):
    joined = " ".join(t or "" for t in texts).lower()
    return any(w in joined for w in VTUBER_WORDS)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---------- 1. 総ざらい ----------
def search_page(params):
    data = api_get("search", dict(params, part="snippet", type="video", maxResults=50,
                                  regionCode=REGION_CODE, relevanceLanguage=RELEVANCE_LANGUAGE),
                   cost=100)
    return data or {}


def sweep(state, found, stats):
    cursors = state.setdefault("sweep", {})
    floor = NOW - SWEEP_MAX_LAG
    end = NOW - SWEEP_DELAY
    for tag in SWEEP_TAGS:
        c = parse_iso(cursors.get(tag)) or NOW - timedelta(hours=24)
        cursors[tag] = iso(max(c, floor))

    def take(data):
        for item in data.get("items", []):
            sn = dict(item["snippet"], video_id=item["id"]["videoId"])
            found.setdefault(sn["channelId"], sn)
            stats["videos"] += 1

    done_tags = set()
    while budget.run_left() >= 200:
        # いちばん遅れているタグから進める
        todo = [t for t in SWEEP_TAGS if t not in done_tags and parse_iso(cursors[t]) < end]
        if not todo:
            break
        tag = min(todo, key=lambda t: cursors[t])
        start = parse_iso(cursors[tag])
        span = min(end - start, SWEEP_MAX_SPAN)
        # 1回の検索でたどれるのは約500件まで。多すぎる区間は半分にしてやり直す
        while True:
            stop = start + span
            params = {"q": tag, "order": "date",
                      "publishedAfter": iso(start), "publishedBefore": iso(stop)}
            data = search_page(params)
            total = data.get("pageInfo", {}).get("totalResults", 0)
            take(data)
            if total <= SWEEP_SAFE_TOTAL or span <= SWEEP_MIN_SPAN or budget.run_left() < 100:
                break
            span = span / 2
        token, pages = data.get("nextPageToken"), 1
        while token and pages < SWEEP_MAX_PAGES:
            if budget.day_left() < 100:
                break
            data = search_page(dict(params, pageToken=token))
            take(data)
            pages += 1
            token = data.get("nextPageToken")
        if token:
            stats["overflow"] += 1
        cursors[tag] = iso(stop)
        stats["slices"] += 1
        if stop < end and budget.run_left() < 200:
            break
        if stop >= end:
            done_tags.add(tag)


# ---------- 2. 配信検索 ----------
def live_search(state, found):
    i = state.get("live_plan_index", 0) % len(LIVE_PLAN)
    mode, q = LIVE_PLAN[i]
    state["live_plan_index"] = i + 1
    data = search_page({"q": q, "eventType": mode})
    for item in data.get("items", []):
        sn = dict(item["snippet"], video_id=item["id"]["videoId"])
        found.setdefault(sn["channelId"], sn)
    return f"{mode}:{q}"


# ---------- 3. 更新 ----------
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


# ---------- 4. 確認 ----------
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "m": "http://search.yahoo.com/mrss/"}


def rss_latest(ch_id):
    try:
        r = session.get("https://www.youtube.com/feeds/videos.xml",
                        params={"channel_id": ch_id}, timeout=15)
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.content)
        out = []
        for e in root.findall("a:entry", NS)[:RSS_LATEST]:
            t = e.find("m:group/m:thumbnail", NS)
            out.append({"video_id": e.findtext("yt:videoId", namespaces=NS),
                        "title": e.findtext("a:title", namespaces=NS),
                        "thumbnail": t.get("url") if t is not None else None})
        return [x for x in out if x["video_id"]]
    except Exception:
        return None


def fetch_videos(ids):
    out = {}
    for batch in chunks(ids):
        data = api_get("videos", {"part": "snippet,liveStreamingDetails", "id": ",".join(batch)}, cost=1)
        for v in (data or {}).get("items", []):
            out[v["id"]] = v
    return out


def main():
    global budget
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = load(DB_PATH, {})
    state = load(STATE_PATH, {})
    budget = Budget(state)
    rejected = state.setdefault("rejected", {})
    for cid in [c for c, t in rejected.items() if NOW - parse_iso(t) > timedelta(days=30)]:
        rejected.pop(cid)

    found = {}
    stats = {"videos": 0, "slices": 0, "overflow": 0, "new": 0}
    live_q = None
    try:
        # 配信検索は毎回1つ、残りを総ざらいに使う（本数の更新などの分は残しておく）
        if budget.run_left() >= 100:
            live_q = live_search(state, found)
        reserve = 60 + len(db) // 40
        budget.run_cap -= reserve
        sweep(state, found, stats)
        budget.run_cap += reserve

        # 新しく見つかったチャンネルを登録
        new_ids = [c for c in found if c not in db and c not in rejected]
        for cid, ch in fetch_channels(new_ids).items():
            st, sn = ch["statistics"], ch["snippet"]
            v = found[cid]
            ok = (not st.get("hiddenSubscriberCount") and "subscriberCount" in st
                  and int(st["subscriberCount"]) <= POOL_MAX_SUBSCRIBERS
                  and looks_vtuber(v.get("title"), v.get("description"), sn["title"], sn.get("description")))
            if ok:
                db[cid] = {"first_seen": iso(NOW), "counts_at": None}
                stats["new"] += 1
            else:
                rejected[cid] = iso(NOW)

        # 全チャンネルの登録者数・名前・アイコンを更新。上限を超えたら外す
        for cid, ch in fetch_channels(list(db)).items():
            st, sn = ch["statistics"], ch["snippet"]
            subs = int(st.get("subscriberCount", 0))
            if st.get("hiddenSubscriberCount") or subs > POOL_MAX_SUBSCRIBERS:
                db.pop(cid, None)
                rejected[cid] = iso(NOW)
                continue
            db[cid].update(name=sn["title"], icon=thumb(sn.get("thumbnails")), subscribers=subs)
        for cid in [c for c, r in db.items() if "name" not in r]:
            db.pop(cid)  # 削除・非公開になったチャンネル

        # RSSで新しい動画を見つけ、まだ確定していないものだけAPIで状態を確認
        ids = list(db)
        with ThreadPoolExecutor(max_workers=16) as ex:
            feeds = dict(zip(ids, ex.map(rss_latest, ids)))
        check = set()
        for cid, entries in feeds.items():
            rec = db[cid]
            done = set(rec.get("done", []))
            if entries:
                rec["latest"] = entries[0]
                check.update(e["video_id"] for e in entries if e["video_id"] not in done)
            if rec.get("live"):
                check.add(rec["live"]["video_id"])
        for cid, sn in found.items():
            if cid in db and sn.get("liveBroadcastContent") in ("live", "upcoming"):
                check.add(sn["video_id"])
        videos = fetch_videos(check)

        by_channel = {}
        for vid in check:
            v = videos.get(vid)
            if v:
                by_channel.setdefault(v["snippet"]["channelId"], []).append(v)

        for cid, rec in db.items():
            vs = by_channel.get(cid, [])
            done = rec.get("done", [])
            live = None
            for v in vs:
                lbc = v["snippet"].get("liveBroadcastContent")
                d = v.get("liveStreamingDetails", {})
                if lbc == "live":
                    live = live or v
                elif lbc == "none":
                    if v["id"] not in done:
                        done.append(v["id"])
                    end = parse_iso(d.get("actualEndTime"))
                    prev = parse_iso(rec.get("last_live_at"))
                    if end and (not prev or end > prev):
                        rec["last_live_at"] = iso(end)
            # 消えた動画も確定扱いにして、次から確認しない
            for e in (feeds.get(cid) or []):
                if e["video_id"] in check and e["video_id"] not in videos and e["video_id"] not in done:
                    done.append(e["video_id"])
            rec["done"] = done[-20:]

            was_live = bool(rec.get("live"))
            if live:
                d = live.get("liveStreamingDetails", {})
                rec["live"] = {
                    "video_id": live["id"],
                    "title": live["snippet"]["title"],
                    "thumbnail": thumb(live["snippet"].get("thumbnails")),
                    "viewers": int(d["concurrentViewers"]) if "concurrentViewers" in d else None,
                    "started_at": d.get("actualStartTime"),
                }
                rec["is_live"] = True
                rec["last_live_at"] = iso(NOW)
                if not was_live:
                    rec["counts_at"] = None  # 配信が始まったらライブ本数を数え直す
            else:
                rec.pop("live", None)
                rec["is_live"] = False

        # 本数の更新（未取得 → 配信中 → 古い順。この回の残りユニットの範囲で）
        def due(rec):
            t = parse_iso(rec.get("counts_at"))
            return t is None or NOW - t > COUNTS_REFRESH
        queue = sorted((c for c, r in db.items() if due(r)),
                       key=lambda c: (db[c].get("counts_at") is not None,
                                      not db[c].get("is_live"),
                                      db[c].get("counts_at") or ""))
        for cid in queue:
            if budget.run_left() < 3:
                break
            refresh_counts(cid, db[cid])

    except QuotaExceeded:
        print("APIの1日の上限に達したため、ここまでの結果で保存します。")

    state["last_run"] = iso(NOW)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=0, sort_keys=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)

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
            item["live"] = dict(r["live"], title=html.unescape(r["live"]["title"] or ""))
        if r.get("latest"):
            item["latest"] = dict(r["latest"], title=html.unescape(r["latest"].get("title") or ""))
        channels.append(item)

    out = {"updated_at": iso(datetime.now(timezone.utc)), "pool_max_subscribers": POOL_MAX_SUBSCRIBERS,
           "sweep": state.get("sweep", {}), "live_query": live_q,
           "quota_used": budget.run_used, "channels": channels}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    live_n = sum(1 for c in channels if c["is_live"])
    print(f"総ざらい {stats['slices']}区切り・{stats['videos']}本"
          f"{'（500件超えの区切り ' + str(stats['overflow']) + '）' if stats['overflow'] else ''}"
          f" / 配信検索 {live_q} / 新規 {stats['new']} / 蓄積 {len(channels)}（配信中 {live_n}）"
          f" / API 今回{budget.run_used}・今日{budget.q['used']}ユニット")


if __name__ == "__main__":
    API_KEY = os.environ.get("YOUTUBE_API_KEY")
    if not API_KEY:
        sys.exit("環境変数 YOUTUBE_API_KEY を設定してください")
    main()
