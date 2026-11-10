"""Real Chromium smoke checks at desktop and mobile widths; no YouTube API calls."""
import contextlib
import datetime
import functools
import http.server
import json
import os
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1] / "site"
NOW = datetime.datetime.now(datetime.timezone.utc).isoformat()
CHANNELS = [
    {
        "id": "UC" + str(i).zfill(22),
        "name": f"VTuber {i}",
        "subscribers": i,
        "lives": i % 10,
        "videos": i % 50,
        "shorts": 0,
        "is_live": False,
        "latest": None,
    }
    for i in range(351)
]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(
    ("127.0.0.1", 0),
    functools.partial(QuietHandler, directory=str(ROOT)),
)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for width, height in [(1440, 900), (390, 844)]:
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(
                "**/data.json?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"updated_at": NOW, "channels": CHANNELS}),
                ),
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
            page.locator("#meta").get_by_text("最終更新").wait_for(timeout=10000)
            page.locator("#st-all").click()
            assert page.locator(".card").count() == 300, f"{width}: initial pagination"
            page.locator("#showMore").click()
            assert page.locator(".card").count() == 351, f"{width}: expanded pagination"
            page.locator("#f-subs-min").fill("350")
            assert page.locator(".card").count() == 1, f"{width}: subscriber filtering"
            assert "VTuber 350" in page.locator(".card").first.inner_text()
            page.locator("#reset").click()
            assert page.locator("#f-subs-min").input_value() == "", f"{width}: reset"
            assert not errors, f"{width}: JS errors: {errors}"
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth + 1"
            ), f"{width}: horizontal overflow"
            page.close()
            print(f"Chromium smoke OK: {width}x{height}")
        if os.environ.get("CHECK_PUBLIC_SITE") == "1":
            # Exercise the actual deployed HTML and dataset, not mocked fixtures.
            public_url = "https://takuminurufff-collab.github.io/vtuber-live-finder/"
            for width, height in [(1440, 900), (390, 844)]:
                page = browser.new_page(viewport={"width": width, "height": height})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                response = page.goto(public_url, wait_until="domcontentloaded", timeout=30000)
                assert response and response.ok, f"{width}: public site HTTP error"
                page.locator("#meta").get_by_text("最終更新").wait_for(timeout=30000)
                page.locator("#st-all").click()
                assert page.locator(".card").count() > 0, f"{width}: empty public catalog"
                page.locator("#f-subs-min").fill("0")
                page.locator("#reset").click()
                assert page.locator("#f-subs-min").input_value() == "", f"{width}: public reset"
                assert not errors, f"{width}: public JS errors: {errors}"
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                ), f"{width}: public horizontal overflow"
                page.close()
                print(f"Public Chromium smoke OK: {width}x{height}")
        browser.close()
finally:
    server.shutdown()
