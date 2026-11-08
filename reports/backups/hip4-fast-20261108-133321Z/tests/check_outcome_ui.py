"""Headless multi-process Outcome UI checks; every network request is mocked."""
import mimetypes
from pathlib import Path
import tempfile
from urllib.parse import urlparse
from playwright.sync_api import expect, sync_playwright

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for width in (1440, 390):
                bots, starts, stops, errors = [], [], [], []
                fail_start, fail_stop = [False], [False]
                auto_state = [{"ready": False, "connected": True, "warming_up": True,
                               "observed_sec": 15, "markets_observed": 200, "candidates": []}]
                context = browser.new_context(viewport={"width": width, "height": 1000})
                context.add_init_script("""window.WebSocket = class {
                    static OPEN = 1; static CONNECTING = 0; readyState = 1;
                    constructor() { window.__testSocket = this; } close() {}
                };""")

                def status():
                    return {"type": "worldcup_stats", "bots": bots, "can_start": True,
                            "running": any(b["running"] for b in bots), "phase": "running",
                            "cleanup_confirmed": all(b["cleanup_confirmed"] for b in bots),
                            "active_markets": sum(b["running"] for b in bots), "duration": "00:01:00",
                            "volume_usd": sum(b["volume_usd"] for b in bots), "fees": sum(b["fees"] for b in bots),
                            "net_pnl": sum(b["net_pnl"] for b in bots), "fills": sum(b["fills"] for b in bots), "cost_per_1m": 1000}

                def route_request(route):
                    url = urlparse(route.request.url)
                    if url.hostname != "zeus.test":
                        route.fulfill(status=200, body="")
                        return
                    path = url.path
                    if path.startswith("/api/"):
                        result = {}
                        if path == "/api/outcomes/start":
                            payload = route.request.post_data_json
                            starts.append(payload)
                            if fail_start[0]:
                                route.fulfill(status=400, json={"message": "Offline test rejection"})
                                return
                            bot = {"session_id": f"session-{len(starts)}", "market": payload["market"],
                                   "market_name": "Spurs" if payload["market"] == "#14881" else "Lakers",
                                   "pid": 100 + len(starts), "config": payload, "running": True,
                                   "phase": "running", "cleanup_confirmed": False, "can_start": False,
                                   "volume_usd": 100, "fees": 0.1, "net_pnl": -0.1, "fills": 2,
                                   "duration": "00:01:00", "position_usd": 0, "position_known": True,
                                   "cost_per_1m": 1000, "accounting_complete": True}
                            bots[:] = [b for b in bots if b["market"] != bot["market"]] + [bot]
                            result = {"status": "ok", "session_id": bot["session_id"], "market": bot["market"], "pid": bot["pid"]}
                        elif path == "/api/outcomes/stop":
                            payload = route.request.post_data_json
                            stops.append(payload)
                            if fail_stop[0]:
                                route.fulfill(status=400, json={"message": "Stop not confirmed"})
                                return
                            for bot in bots:
                                if not payload.get("session_id") or bot["session_id"] == payload["session_id"]:
                                    bot.update(running=False, phase="stopped", cleanup_confirmed=True, can_start=True)
                            result = {"status": "ok", "state": status()}
                        elif path == "/api/outcomes/auto":
                            result = {**auto_state[0], "updated_at": __import__("time").time()}
                        elif path == "/api/outcomes/status": result = status()
                        elif path == "/api/outcomes/markets":
                            result = {"outcomes": [{"key": "#14881", "id": 1488, "name": "Spurs", "mid0": 0.87, "mid1": 0.13},
                                                   {"key": "#14751", "id": 1475, "name": "Lakers", "mid0": 0.6, "mid1": 0.4}], "categories": []}
                        elif path == "/api/grid/status": result = {"type": "grid_stats", "running": False, "bots": []}
                        elif path == "/api/grid/tokens": result = {"tokens": []}
                        elif path == "/api/sessions/reconcile": result = {"can_start": True, "unresolved": []}
                        route.fulfill(json=result)
                        return
                    file = FRONTEND / (path.lstrip("/").removeprefix("frontend/") or "index.html")
                    if file.name == "config.js": route.fulfill(body="window.ZEUS_CONFIG = {};")
                    elif file.is_file(): route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream")
                    else: route.fulfill(status=404)

                context.route("**/*", route_request)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://zeus.test/")
                page.locator("#tab-outcome").click()
                start = page.locator("#outcome-start-btn")
                expect(start).to_be_enabled()
                expect(start).to_contain_text("Choose market")
                start.click()
                expect(page.locator("#outcome-market-search")).to_be_focused()
                page.keyboard.press("Escape")

                def select(name):
                    page.locator("#outcome-market-trigger").click()
                    expect(page.locator("#outcome-market-options")).not_to_contain_text("All markets")
                    page.locator("#outcome-market-options").get_by_role("option", name=name).click()

                select("Spurs")
                expect(start).to_be_enabled()
                expect(page.locator("#outcome-side-help")).to_be_hidden()
                page.locator("#outcome-details summary").focus()
                page.keyboard.press("Enter")
                expect(page.locator("#outcome-side-help")).to_be_visible()
                page.keyboard.press("Enter")
                expect(page.locator("#outcome-side-help")).to_be_hidden()
                yes = page.locator("#outcome-side-yes")
                no = page.locator("#outcome-side-no")
                expect(no).to_be_checked()
                expect(page.locator("#outcome-market-symbol")).to_contain_text("No 13.0%")
                mode = page.locator("#outcome-exec-mode")
                mode.focus()
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                expect(mode).to_have_value("maker")
                expect(page.locator("#outcome-spread")).to_be_hidden()
                maker = page.locator("#outcome-maker-spread")
                maker.fill("-1")
                start.click()
                expect(page.locator("#outcome-form-alert")).to_contain_text("Spread limit")
                assert not starts
                maker.fill("0.007")
                fail_start[0] = True
                start.click()
                expect(page.locator("#outcome-form-alert")).to_contain_text("Offline test rejection")
                fail_start[0] = False
                start.click()
                first = page.locator("#outcome-bots-list .bot-card").filter(has_text="Spurs")
                expect(first).to_contain_text("Limit / Maker")
                expect(first).to_contain_text("0.007%")
                expect(first.locator('[data-f="title"]')).to_have_text("Spurs · No")
                expect(start).to_be_disabled()
                expect(mode).to_be_enabled()
                expect(maker).to_be_enabled()
                no.focus()
                page.keyboard.press("ArrowLeft")
                expect(yes).to_be_checked()
                expect(page.locator("#outcome-market-symbol")).to_contain_text("Yes 87.0%")
                expect(page.locator("#outcome-market")).to_have_value("#14880")
                expect(start).to_be_enabled()  # the existing No bot is independent
                expect(first.locator('[data-f="title"]')).to_have_text("Spurs · No")
                select("Lakers")
                expect(start).to_be_enabled()
                mode.select_option("taker")
                expect(page.locator("#outcome-spread")).to_be_visible()
                start.click()
                second = page.locator("#outcome-bots-list .bot-card").filter(has_text="Lakers")
                expect(second).to_contain_text("Market / Taker")
                expect(second.locator('[data-f="title"]')).to_have_text("Lakers · Yes")
                expect(page.locator("#outcome-bots-list .bot-card")).to_have_count(2)
                assert starts[-2]["market"] == "#14881" and starts[-1]["market"] == "#14750"
                assert starts[-2]["outcome_side"] == "no" and starts[-1]["outcome_side"] == "yes"
                assert starts[-2]["maker_spread_pct"] == 0.007 and "max_spread_pct" not in starts[-2]
                assert starts[-1]["max_spread_pct"] == 0.02
                assert starts[-1]["order_size_usd"] == 50 and starts[-1]["max_position_usd"] == 200

                def push():
                    page.evaluate("value => window.__testSocket.onmessage({data: JSON.stringify(value)})", status())

                bots[0].update(phase="recovering", recovery_error="Insufficient spot balance")
                first.get_by_role("button").focus()
                push()
                expect(first).to_contain_text("auto-recovery")
                expect(first).to_contain_text("Insufficient spot balance")
                expect(first.get_by_role("button")).to_be_focused()
                expect(second).to_contain_text("Active")
                before = len(starts)
                bots[0].update(phase="running", recovery_error="")
                push()
                assert len(starts) == before
                fail_stop[0] = True
                first.get_by_role("button").click()
                expect(page.locator("#toast-stack")).to_contain_text("Stop not confirmed")
                expect(first.get_by_role("button")).to_be_visible()
                fail_stop[0] = False
                first.get_by_role("button").click()
                expect(first).to_have_count(0)
                expect(second.get_by_role("button")).to_be_visible()
                assert stops[-1]["session_id"] == bots[0]["session_id"]
                select("Spurs")
                no.check(force=True)
                expect(start).to_be_enabled()
                bots[0].update(phase="failed", cleanup_confirmed=False, error="Stop not confirmed")
                push()
                expect(start).to_be_disabled()
                expect(page.locator("#sessions-reconcile-btn")).to_be_visible()
                expect(page.locator("#outcome-form-alert")).to_be_hidden()
                page.reload()
                page.locator("#tab-outcome").click()
                expect(page.locator("#outcome-bots-list .bot-card")).to_have_count(2)
                expect(first).to_contain_text("Limit / Maker")
                expect(second).to_contain_text("Market / Taker")
                expect(first.locator('[data-f="title"]')).to_have_text("Spurs · No")
                expect(second.locator('[data-f="title"]')).to_have_text("Lakers · Yes")
                assert len(starts) == before
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Horizontal overflow"
                screenshot = Path(tempfile.gettempdir()) / f"zeus-outcome-processes-{width}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                page.locator("#outcome-stop-btn").click()
                expect(page.locator("#confirm-stop")).to_be_visible()
                page.locator("#confirm-stop-accept").click()
                expect(second).to_have_count(0)
                assert stops[-1] == {}
                expect(page.locator("#outcome-history")).to_have_count(0)
                expect(page.locator("#outcome-idle")).to_be_visible()
                expect(first).to_be_hidden()
                expect(second).to_be_hidden()
                page.locator("#outcome-side-no").check()
                page.locator("#outcome-selection-mode").select_option("auto")
                expect(page.locator("#outcome-auto-status")).to_contain_text("15 / 60")
                expect(mode).to_have_value("maker")
                expect(mode).to_be_disabled()
                expect(maker).to_be_hidden()
                expect(page.locator("#outcome-market-trigger")).to_be_hidden()
                expect(start).to_be_enabled()
                before_auto = len(starts)
                start.click()
                expect(start).to_contain_text("Cancel Auto")
                expect(page.locator("#outcome-order-size")).to_be_disabled()
                expect(page.locator("#outcome-side-yes")).to_be_disabled()
                assert len(starts) == before_auto, "Warmup must not place a start request"
                start.click()
                expect(start).to_contain_text("Start Auto")
                expect(page.locator("#outcome-order-size")).to_be_enabled()
                page.locator("#outcome-auto-spread").fill("5")
                start.click()
                expect(start).to_contain_text("Cancel Auto")
                auto_state[0] = {"ready": True, "connected": True, "warming_up": False,
                                 "observed_sec": 70, "markets_observed": 200, "candidates": [{
                                     "market": "#14881", "name": "Spurs", "volume_usd_1m": 500,
                                     "events_5m": 15, "spread_bps": .2, "queue_proxy_sec": 12}]}
                # The scheduled scan must launch once using the frozen settings.
                expect(page.locator("#outcome-auto-status")).to_contain_text("Shortest queue", timeout=15000)
                expect(page.locator("#outcome-auto-candidates")).to_contain_text("500")
                if page.locator("#outcome-selection-mode").input_value() == "auto":
                    image_dir = FRONTEND.parent / "output" / "playwright"
                    image_dir.mkdir(parents=True, exist_ok=True)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.screenshot(path=str(image_dir / f"outcome-auto-{width}.png"), full_page=True)
                expect(first).to_contain_text("Auto flow")
                expect(first).to_contain_text("best bid / ask")
                assert starts[-1]["selection_mode"] == "auto" and starts[-1]["auto_max_spread_bps"] == 5
                assert starts[-1]["exec_mode"] == "maker" and starts[-1]["outcome_side"] == "no"
                assert starts[-1]["two_sided"] is True and starts[-1]["auto_rotate"] is True
                assert starts[-1]["auto_rotation_after_sec"] == 180
                expect(first).to_contain_text("Bid + Ask")
                page.locator("#outcome-selection-mode").select_option("manual")
                page.locator("#outcome-side-yes").check()
                expect(first.locator('[data-f="title"]')).to_have_text("Spurs · No")
                expect(first).to_contain_text("Auto flow")
                page.screenshot(path=str(Path(tempfile.gettempdir()) / f"zeus-outcome-auto-{width}.png"), full_page=True)
                first.get_by_role("button").click()
                expect(first).to_have_count(0)
                page.locator("#tab-grid").click()
                grid = {"type": "grid_stats", "running": True, "bots": [{
                    "token": "IO_ANTH", "symbol": "io:ANTH", "strategy": "bbo", "running": True,
                    "phase": "running", "error": "", "cleanup_confirmed": False,
                    "warning": "Post-only: book moved, recalculating the rejected side.",
                    "duration": "00:00:47", "mid_price": 1738.2, "volume_usd": 201.63,
                    "net_pnl": 0.01, "fees": 0.01, "fills": 2}]}
                page.evaluate("value => window.__testSocket.onmessage({data: JSON.stringify(value)})", grid)
                card = page.locator("#grid-bots-list .bot-card")
                expect(card).to_contain_text("LIVE")
                expect(card).to_contain_text("Post-only")
                expect(card).not_to_have_class(__import__("re").compile(r"\bfailed\b"))
                expect(card.get_by_role("button", name="Stop IO_ANTH")).to_be_visible()
                grid["bots"][0]["error"] = "Post only order would have immediately matched"
                page.evaluate("value => window.__testSocket.onmessage({data: JSON.stringify(value)})", grid)
                expect(card).not_to_have_class(__import__("re").compile(r"\bfailed\b"))
                grid["bots"][0]["error"] = "bulk_cancel: Order was never placed, already canceled, or filled. asset=200001"
                page.evaluate("value => window.__testSocket.onmessage({data: JSON.stringify(value)})", grid)
                expect(card).to_contain_text("LIVE")
                expect(card).not_to_have_class(__import__("re").compile(r"\bfailed\b"))
                expect(card.get_by_role("button", name="Stop IO_ANTH")).to_be_visible()
                grid["bots"][0].update(error="", warning="Order already filled or cancelled: quote recalculated after sync.")
                page.evaluate("value => window.__testSocket.onmessage({data: JSON.stringify(value)})", grid)
                expect(card).to_contain_text("already filled or cancelled")
                expect(card).not_to_have_class(__import__("re").compile(r"\bfailed\b"))
                page.screenshot(path=str(Path(tempfile.gettempdir()) / f"zeus-bbo-post-only-{width}.png"), full_page=True)
                assert not errors, errors
                print(f"PASS {width}px: independent bots, keyboard, recovery, scoped Stop, Stop all, reload; {screenshot}")
                context.close()
        finally:
            browser.close()


if __name__ == "__main__": main()
