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
import re
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
# 遅延した検索区間は切り捨てず、次回以降に引き継ぐ
SWEEP_DELAY = timedelta(minutes=10)    # 投稿直後は検索に出ないことがあるので少し待つ
SWEEP_MAX_PAGES = 10                   # YouTube検索が返すのは1区切りあたり最大500件

# 配信中・配信予定のキーワード検索（毎回1つずつ順番に）
LIVE_PLAN = [
    ("live", "VTuber"), ("live", "新人VTuber"), ("upcoming", "初配信"),
    ("live", "個人勢VTuber"), ("live", "Vtuber 雑談"), ("upcoming", "新人Vtuber"),
    ("live", "Vtuber 歌枠"), ("live", "Vtuber ゲーム実況"), ("upcoming", "Vtuber デビュー"),
    ("live", "Vライバー"), ("live", "Vtuber 作業"), ("live", "初配信"),
    ("live", "新人V"), ("live", "初見歓迎 VTuber"), ("live", "Vtuber ASMR"),
    ("live", "VTuber 個人"), ("upcoming", "新人V 初配信"),
    ("live", "バーチャル配信者"), ("live", "Vtuber 朝活"),
    ("live", "VTuber マイクラ"), ("upcoming", "VTuber 新人"),
]

# 本数（動画・ショート・ライブ）を数え直す間隔（1チャンネル3ユニット）
COUNTS_REFRESH = timedelta(days=3)
RSS_LATEST = 3
REGION_CODE = "JP"
RELEVANCE_LANGUAGE = "ja"
# VTuberかどうかの判定に使う言葉（動画タイトル・概要・チャンネル名・チャンネル概要のどこか）
VTUBER_WORDS = ["vtuber", "ブイチューバー", "vライバー", "バーチャル", "個人勢", "準備中"]

# 芋づる式：概要欄の「@ハンドル」「チャンネルURL」と、おすすめチャンネル欄から別のVTuberをたどる
SNOWBALL_SECTIONS_PER_RUN = 150   # おすすめチャンネル欄を調べるチャンネル数（1件1ユニット）
SNOWBALL_HANDLES_PER_RUN = 150    # @ハンドルをチャンネルに変換する数（1件1ユニット）
SNOWBALL_SECTIONS_EVERY = timedelta(days=7)
RECHECK_REJECTED_PER_RUN = 25
RECHECK_REJECTED_INTERVAL = timedelta(days=14)
# 企業勢の判定：概要欄に「所属」と、下の事務所名の両方があれば企業勢とみなす
# （「所属」だけだとサーバーやグループの所属まで外してしまうため）
# 「無所属」「個人勢」などが書かれていれば個人勢として扱う
CORP_AGENCIES = ["ホロライブ", "hololive", "ホロスターズ", "holostars", "にじさんじ", "nijisanji",
                 "ぶいすぽ", "vspo", "ななしいんく", "774inc", "のりプロ", "noripro", "あおぎり高校",
                 "ネオポルテ", "neo-porte", "riot music", "ハコネクト", "re:act", "upd8",
                 ".live", "どっとライブ", "ブイアパ", "varium", "パレプロ", "ミリプロ", "深層組",
                 "有閑喫茶", "あにまーれ", "プロプロ", "ぷろぐれす", "vshojo", "phase connect", "idol corp"]
INDIE_WORDS = ["無所属", "個人勢", "個人vtuber", "個人で活動", "個人ｖ", "個人v"]

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


def is_corporate(*texts):
    t = " ".join(x or "" for x in texts).lower()
    if any(w in t for w in INDIE_WORDS):
        return False
    return "所属" in t and any(a in t for a in CORP_AGENCIES)


UC_RE = re.compile(r"youtube\.com/channel/(UC[\w-]{22})")
HANDLE_RE = re.compile(r"(?:youtube\.com/|(?<![\w@.]))@([\w.\-\u3040-\u30ff\u4e00-\u9fff]{3,30})")


def mentions(text):
    """概要欄などからチャンネルIDと@ハンドルを取り出す"""
    text = text or ""
    ids = set(UC_RE.findall(text))
    handles = {h.rstrip(".").lower() for h in HANDLE_RE.findall(text)}
    return ids, handles


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
    pending = state.setdefault("sweep_pending", {})
    end = NOW - SWEEP_DELAY
    for tag in SWEEP_TAGS:
        c = parse_iso(cursors.get(tag)) or NOW - timedelta(hours=24)
        # 古いカーソルも保持し、処理しきれない区間を消去しない。
        cursors[tag] = iso(c)

    def take(data):
        for item in data.get("items", []):
            sn = dict(item["snippet"], video_id=item["id"]["videoId"])
            found.setdefault(sn["channelId"], sn)
            stats["videos"] += 1

    done_tags = set()
    while budget.run_left() >= 200:
        todo = [t for t in SWEEP_TAGS if t not in done_tags and
                (t in pending or parse_iso(cursors[t]) < end)]
        if not todo:
            break
        tag = min(todo, key=lambda t: cursors[t])
        start = parse_iso(cursors[tag])
        work = pending.get(tag)

        if work and work.get("start") != cursors[tag]:
            # 進捗位置が外部で変わったときは古いページトークンを捨てる。
            pending.pop(tag, None)
            work = None

        if work and work.get("restart"):
            # 500件上限・期限切れトークンによる再試行。開始時点でフラグを解除。
            pending.pop(tag, None)
            stop = parse_iso(work["stop"])
            params = {"q": tag, "order": "date",
                      "publishedAfter": cursors[tag], "publishedBefore": work["stop"]}
            data = search_page(params)
            take(data)
            token, pages = data.get("nextPageToken"), 1
        elif work:
            stop = parse_iso(work["stop"])
            token = work["next_page"]
            pages = work["pages"]
            params = {"q": tag, "order": "date",
                      "publishedAfter": cursors[tag], "publishedBefore": work["stop"]}
            # 最初のページは既に取得済み。保存されたトークンから再開する。
            if budget.run_left() < 100 or budget.day_left() < 100:
                break
            try:
                data = search_page(dict(params, pageToken=token))
            except RuntimeError as exc:
                if "invalidPageToken" not in str(exc):
                    raise
                # ページトークン失効時は区間を保持し、次回は先頭から再検索。
                pending[tag] = {"start": cursors[tag], "stop": iso(stop),
                                "next_page": None, "pages": 0, "restart": True}
                done_tags.add(tag)
                continue
            take(data)
            pages += 1
            token = data.get("nextPageToken")
        else:
            span = min(end - start, SWEEP_MAX_SPAN)
            # 500件超の区間は先に分割し、検索上限到達をできるだけ避ける。
            while True:
                stop = start + span
                params = {"q": tag, "order": "date",
                          "publishedAfter": iso(start), "publishedBefore": iso(stop)}
                data = search_page(params)
                total = data.get("pageInfo", {}).get("totalResults", 0)
                if (total <= SWEEP_SAFE_TOTAL or span <= SWEEP_MIN_SPAN
                        or budget.run_left() < 100):
                    take(data)
                    break
                span = span / 2
            token, pages = data.get("nextPageToken"), 1

        while token and pages < SWEEP_MAX_PAGES:
            if budget.run_left() < 100 or budget.day_left() < 100:
                break
            try:
                data = search_page(dict(params, pageToken=token))
            except RuntimeError as exc:
                if "invalidPageToken" not in str(exc):
                    raise
                # 実行中のページトークン失効でも区間を捨てず、次回先頭から取り直す。
                pending[tag] = {"start": cursors[tag], "stop": iso(stop),
                                "next_page": None, "pages": 0, "restart": True}
                done_tags.add(tag)
                token = None
                break
            take(data)
            pages += 1
            token = data.get("nextPageToken")

        if pending.get(tag, {}).get("restart"):
            stats["overflow"] += 1
            continue

        if token:
            stats["overflow"] += 1
            if pages >= SWEEP_MAX_PAGES:
                # YouTubeの検索上限を超えたときはページトークンを再利用しない。
                # 可能なら区間をさらに分割して次回やり直す。
                span = stop - start
                if span > SWEEP_MIN_SPAN:
                    pending.pop(tag, None)
                    pending[tag] = {"start": cursors[tag],
                                    "stop": iso(start + span / 2),
                                    "next_page": None, "pages": 0,
                                    "restart": True}
                else:
                    state.setdefault("sweep_blocked", {})[tag] = {
                        "start": cursors[tag], "stop": iso(stop),
                        "reason": "500-result-limit"}
                    pending.pop(tag, None)
            else:
                pending[tag] = {"start": cursors[tag], "stop": iso(stop),
                                "next_page": token, "pages": pages}
            # 予算が十分あっても同じ失敗区間で回り続けないようにする。
            # 続きのページや分割区間は次回の実行で再開する。
            done_tags.add(tag)
            continue

        pending.pop(tag, None)
        state.setdefault("sweep_blocked", {}).pop(tag, None)
        cursors[tag] = iso(stop)
        stats["slices"] += 1
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
    """3項目をすべて取得できた場合だけ更新する。

    API枠超過・通信エラーなどが途中で発生しても、以前の値と更新日時を
    一部だけ書き換えない。次回の更新で安全に再試行できる。
    """
    suffix = ch_id[2:]
    videos = playlist_total("UULF" + suffix)  # 通常の動画
    shorts = playlist_total("UUSH" + suffix)  # ショート
    lives = playlist_total("UULV" + suffix)   # ライブ
    rec.update(videos=videos, shorts=shorts, lives=lives, counts_at=iso(NOW))


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
        out, ids, handles = [], set(), set()
        for n, e in enumerate(root.findall("a:entry", NS)):
            desc = e.findtext("m:group/m:description", namespaces=NS) or ""
            i, h = mentions(desc + " " + (e.findtext("a:title", namespaces=NS) or ""))
            ids |= i
            handles |= h
            if n < RSS_LATEST:
                t = e.find("m:group/m:thumbnail", NS)
                out.append({"video_id": e.findtext("yt:videoId", namespaces=NS),
                            "title": e.findtext("a:title", namespaces=NS),
                            "thumbnail": t.get("url") if t is not None else None})
        ids.discard(ch_id)
        return {"entries": [x for x in out if x["video_id"]], "ids": ids, "handles": handles}
    except Exception:
        return None


def featured_channels(ch_id):
    data = api_get("channelSections", {"part": "contentDetails", "channelId": ch_id}, cost=1)
    out = set()
    for sec in (data or {}).get("items", []):
        out.update(sec.get("contentDetails", {}).get("channels", []))
    out.discard(ch_id)
    return out


def resolve_handle(handle):
    data = api_get("channels", {"part": "id", "forHandle": "@" + handle}, cost=1)
    items = (data or {}).get("items", [])
    return items[0]["id"] if items else ""


def fetch_videos(ids):
    out = {}
    for batch in chunks(ids):
        data = api_get("videos", {"part": "snippet,liveStreamingDetails", "id": ",".join(batch)}, cost=1)
        for v in (data or {}).get("items", []):
            out[v["id"]] = v
    return out


def subscriber_count(statistics):
    """非公開・未返却はNone。登録者0人とは明確に区別する。"""
    if statistics.get("hiddenSubscriberCount") or statistics.get("subscriberCount") is None:
        return None
    return int(statistics["subscriberCount"])


def within_pool_limit(subscribers):
    """登録者非公開は除外せず、上限値が判明している場合だけ制限する。"""
    return subscribers is None or subscribers <= POOL_MAX_SUBSCRIBERS


def recheck_rejected(db, state, rejected, stats):
    """既存の拒否履歴を、空きクォータ内で少しずつ再確認する。"""
    checked = state.setdefault("rejected_rechecked", {})
    for cid in list(checked):
        if cid not in rejected:
            del checked[cid]

    if budget.run_left() < 2 or budget.day_left() < 2:
        return
    due = []
    for cid in rejected:
        last = parse_iso(checked.get(cid))
        if last is None or NOW - last >= RECHECK_REJECTED_INTERVAL:
            due.append(cid)
    due.sort(key=lambda cid: (checked.get(cid) is not None, checked.get(cid) or "", cid))
    due = due[:RECHECK_REJECTED_PER_RUN]
    if not due:
        return

    channels = fetch_channels(due)
    for cid in due:
        checked[cid] = iso(NOW)
        ch = channels.get(cid)
        if ch is None:
            continue
        sn, st = ch["snippet"], ch["statistics"]
        # 以前は企業勢と判定されるだけで除外されていたので再検査する。
        vtuber = looks_vtuber(sn["title"], sn.get("description")) or is_corporate(
            sn["title"], sn.get("description"))
        subscribers = subscriber_count(st)
        if vtuber and within_pool_limit(subscribers):
            db[cid] = {
                "first_seen": iso(NOW), "counts_at": None, "via": "rechecked",
                "name": sn["title"], "icon": thumb(sn.get("thumbnails")),
                "subscribers": subscribers,
            }
            rejected.pop(cid, None)
            checked.pop(cid, None)
            stats["recovered"] += 1


# 新規・未取得チャンネルの本数取得を総ざらい検索より優先する。
# APIはチャンネルあたり3ユニット。既存の予算制御の範囲で実施する。
EARLY_COUNTS_PER_RUN = 20


def counts_due(rec):
    if any(rec.get(k) is None for k in ("videos", "shorts", "lives")):
        return True
    t = parse_iso(rec.get("counts_at"))
    return t is None or NOW - t > COUNTS_REFRESH


def refresh_due_counts(db, max_channels=None):
    """未取得の本数を優先してAPI枠の範囲で更新し、実行件数を返す。"""
    queue = sorted(
        (cid for cid, rec in db.items() if counts_due(rec)),
        key=lambda cid: (
            all(db[cid].get(k) is not None for k in ("videos", "shorts", "lives")),
            not db[cid].get("is_live"),
            db[cid].get("counts_at") or "",
            cid,
        ),
    )
    refreshed = 0
    for cid in queue:
        if max_channels is not None and refreshed >= max_channels:
            break
        if budget.run_left() < 3 or budget.day_left() < 3:
            break
        refresh_counts(cid, db[cid])
        refreshed += 1
    return refreshed


def main():
    global budget
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = load(DB_PATH, {})
    state = load(STATE_PATH, {})
    budget = Budget(state)
    rejected = state.setdefault("rejected", {})
    # 拒否済み候補は30日で破棄しない。再検査キューで周期的に再評価する。

    found = {}
    stats = {"videos": 0, "slices": 0, "overflow": 0, "new": 0, "snow": 0, "recovered": 0}
    live_q = None
    try:
        # 1回のAPI枠が250超のときだけ未取得本数を先行補完する。
        # タグ検索に必要な200ユニット＋その他処理向け50ユニットを先に保護。
        early_cap = min(EARLY_COUNTS_PER_RUN, max(0, (budget.run_left() - 250) // 3))
        counts_early = refresh_due_counts(db, max_channels=early_cap)

        # 最新mainの「タグの総ざらい優先」を維持し、検索には最大限200ユニットを確保。
        requested_reserve = (30 + len(db) // 40
                             + min(SNOWBALL_SECTIONS_PER_RUN + SNOWBALL_HANDLES_PER_RUN,
                                   budget.run_cap // 4))
        reserve = min(requested_reserve, max(0, budget.run_left() - 200))
        budget.run_cap -= reserve
        try:
            sweep(state, found, stats)
            if budget.run_left() >= 100:
                live_q = live_search(state, found)
        finally:
            budget.run_cap += reserve

        # 新しく見つかったチャンネルを登録
        new_ids = [c for c in found if c not in db and c not in rejected]
        for cid, ch in fetch_channels(new_ids).items():
            st, sn = ch["statistics"], ch["snippet"]
            v = found[cid]
            subscribers = subscriber_count(st)
            ok = (within_pool_limit(subscribers)
                  and looks_vtuber(v.get("title"), v.get("description"), sn["title"], sn.get("description")))
            # 企業勢も対象。事務所所属を理由に候補から除外しない。
            if ok:
                db[cid] = {"first_seen": iso(NOW), "counts_at": None,
                           "subscribers": subscribers}
                stats["new"] += 1
            else:
                rejected[cid] = iso(NOW)

        recheck_rejected(db, state, rejected, stats)

        # 全チャンネルの登録者数・名前・アイコンを更新。上限を超えたら外す
        for cid, ch in fetch_channels(list(db)).items():
            st, sn = ch["statistics"], ch["snippet"]
            subs = subscriber_count(st)
            if not within_pool_limit(subs):
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
        for cid, feed in feeds.items():
            rec = db[cid]
            done = set(rec.get("done", []))
            entries = (feed or {}).get("entries")
            if entries:
                rec["latest"] = entries[0]
                check.update(e["video_id"] for e in entries if e["video_id"] not in done)
            if rec.get("live"):
                check.add(rec["live"]["video_id"])
        for cid, sn in found.items():
            if cid in db and sn.get("liveBroadcastContent") in ("live", "upcoming"):
                check.add(sn["video_id"])
        videos = fetch_videos(check)

        # 芋づる式：概要欄の言及と、おすすめチャンネル欄から候補を集める
        cand = set()
        handle_cache = state.setdefault("handles", {})
        pending_handles = set()
        for feed in feeds.values():
            if feed:
                cand |= feed["ids"]
                pending_handles |= {h for h in feed["handles"] if h not in handle_cache}
        for v in videos.values():
            i, h = mentions(v["snippet"].get("description"))
            cand |= i
            pending_handles |= {x for x in h if x not in handle_cache}
        for h in sorted(pending_handles)[:SNOWBALL_HANDLES_PER_RUN]:
            if budget.run_left() < 50:
                break
            handle_cache[h] = resolve_handle(h)
        cand |= {c for c in handle_cache.values() if c}
        sec_queue = sorted(db, key=lambda c: db[c].get("sections_at") or "")
        n = 0
        for cid in sec_queue:
            t = parse_iso(db[cid].get("sections_at"))
            if (t and NOW - t < SNOWBALL_SECTIONS_EVERY) or n >= SNOWBALL_SECTIONS_PER_RUN or budget.run_left() < 50:
                break
            cand |= featured_channels(cid)
            db[cid]["sections_at"] = iso(NOW)
            n += 1
        cand = [c for c in cand if c not in db and c not in rejected]
        for cid, ch in fetch_channels(cand).items():
            st, sn = ch["statistics"], ch["snippet"]
            subscribers = subscriber_count(st)
            if (within_pool_limit(subscribers)
                    and looks_vtuber(sn["title"], sn.get("description"))):
                db[cid] = {"first_seen": iso(NOW), "counts_at": None, "via": "snowball",
                           "name": sn["title"], "icon": thumb(sn.get("thumbnails")),
                           "subscribers": subscribers}
                stats["snow"] += 1
            else:
                rejected[cid] = iso(NOW)
        if len(handle_cache) > 20000:
            for k in list(handle_cache)[:5000]:
                handle_cache.pop(k)

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
            for e in ((feeds.get(cid) or {}).get("entries") or []):
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

        # 最後に残ったAPI枠でも本数を更新する。先ほど更新した対象は除外される。
        counts_late = refresh_due_counts(db)

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
          f" / 配信検索 {live_q} / 新規 {stats['new']}・芋づる {stats['snow']}・再発見 {stats['recovered']} / 蓄積 {len(channels)}（配信中 {live_n}）"
          f" / API 今回{budget.run_used}・今日{budget.q['used']}ユニット")


if __name__ == "__main__":
    API_KEY = os.environ.get("YOUTUBE_API_KEY")
    if not API_KEY:
        sys.exit("環境変数 YOUTUBE_API_KEY を設定してください")
    main()
