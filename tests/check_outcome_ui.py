"""Headless UI checks with all network traffic intercepted; no live backend."""
import mimetypes
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for width in (1440, 390):
                state = {"running": False, "config": {}, "type": "worldcup_stats", "phase": "stopped", "cleanup_confirmed": True, "can_start": True}
                submissions = []
                errors = []
                fail_start = [False]
                fail_stop = [False]
                context = browser.new_context(viewport={"width": width, "height": 1000})
                context.add_init_script("""
                    window.WebSocket = class {
                        static OPEN = 1; static CONNECTING = 0;
                        readyState = 1;
                        constructor() { window.__testSocket = this; }
                        close() {}
                    };
                """)

                def route_request(route):
                    url = urlparse(route.request.url)
                    if url.hostname != "zeus.test":
                        route.fulfill(status=200, body="")
                        return
                    path = url.path
                    if path.startswith("/api/"):
                        result = {}
                        if path == "/api/outcomes/start":
                            body = route.request.post_data_json
                            submissions.append(body)
                            if fail_start[0]:
                                route.fulfill(status=400, json={"message": "Offline test rejection"})
                                return
                            state.update(running=True, config=body, phase="running", cleanup_confirmed=False, can_start=False)
                        elif path == "/api/outcomes/stop":
                            if fail_stop[0]:
                                route.fulfill(status=400, json={"message": "Stop not confirmed"})
                                return
                            state.update(running=False, phase="stopped", cleanup_confirmed=True, can_start=True)
                        elif path == "/api/sessions/reconcile":
                            result = {"can_start": state["cleanup_confirmed"], "unresolved": [] if state["cleanup_confirmed"] else ["outcome"]}
                        elif path == "/api/outcomes/status":
                            result = state
                        elif path == "/api/outcomes/markets":
                            result = {"outcomes": [], "categories": []}
                        elif path == "/api/grid/status":
                            result = {"type": "grid_stats", "running": False, "bots": []}
                        elif path == "/api/grid/tokens":
                            result = {"tokens": []}
                        route.fulfill(json=result)
                        return
                    file = FRONTEND / (path.lstrip("/") or "index.html")
                    if file.is_file():
                        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream")
                    else:
                        route.fulfill(status=404)

                context.route("**/*", route_request)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://zeus.test/")
                page.locator("#tab-outcome").click()
                mode = page.locator("#outcome-exec-mode")
                expect(mode).to_have_value("taker")
                expect(page.locator("#outcome-spread")).to_be_visible()
                mode.focus()
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                expect(mode).to_have_value("maker")
                expect(page.locator("#outcome-spread")).to_be_hidden()
                expect(page.locator("#outcome-spread")).to_be_disabled()
                maker_spread = page.locator("#outcome-maker-spread")
                expect(maker_spread).to_be_visible()
                expect(maker_spread).to_have_value("0.005")
                maker_spread.fill("-1")
                page.locator("#outcome-start-btn").click()
                expect(page.locator("#outcome-form-alert")).to_contain_text("Spread Limit")
                assert not submissions
                maker_spread.fill("0.007")
                # An invalid hidden Taker filter must not block Limit startup.
                page.locator("#outcome-spread").evaluate("el => el.value = ''")
                fail_start[0] = True
                page.locator("#outcome-start-btn").click()
                expect(page.locator("#outcome-form-alert")).to_contain_text("Offline test rejection")
                expect(mode).to_be_enabled()
                fail_start[0] = False
                page.locator("#outcome-start-btn").click()
                expect(mode).to_be_disabled()
                expect(page.locator("#outcome-active-mode")).to_have_text("Limit / Maker")
                assert submissions[-1]["exec_mode"] == "maker"
                assert "max_spread_pct" not in submissions[-1]
                assert submissions[-1]["maker_spread_pct"] == 0.007
                expect(maker_spread).to_be_disabled()
                assert submissions[-1]["order_size_usd"] == 50
                assert submissions[-1]["max_position_usd"] == 200
                page.reload()
                page.locator("#tab-outcome").click()
                expect(mode).to_have_value("maker")
                expect(mode).to_be_disabled()
                expect(page.locator("#outcome-spread")).to_be_hidden()
                expect(maker_spread).to_have_value("0.007")
                expect(maker_spread).to_be_disabled()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Horizontal overflow"
                screenshot = Path(tempfile.gettempdir()) / f"zeus-outcome-limit-{width}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                page.locator("#outcome-stop-btn").click()
                expect(mode).to_be_enabled()
                mode.select_option("taker")
                expect(page.locator("#outcome-spread")).to_be_visible()
                expect(maker_spread).to_be_hidden()
                page.locator("#outcome-start-btn").click()
                expect(mode).to_be_disabled()
                assert submissions[-1]["exec_mode"] == "taker"
                assert submissions[-1]["max_spread_pct"] == 0.02
                assert "maker_spread_pct" not in submissions[-1]
                before_recovery = len(submissions)
                state.update(phase="recovering", recovery_error="Insufficient spot balance; synchronisation")
                page.evaluate("value => window.__testSocket.onmessage({data: JSON.stringify(value)})", state)
                expect(page.locator("#outcome-run-banner")).to_contain_text("reprise automatique")
                expect(page.locator("#outcome-form-alert")).to_contain_text("Insufficient spot balance")
                expect(page.locator("#outcome-start-btn")).to_be_disabled()
                expect(page.locator("#outcome-stop-btn")).to_be_enabled()
                expect(mode).to_be_disabled()
                state.update(phase="running", recovery_error="")
                page.evaluate("value => window.__testSocket.onmessage({data: JSON.stringify(value)})", state)
                expect(page.locator("#outcome-run-banner")).to_contain_text("Session active")
                expect(page.locator("#outcome-form-alert")).to_be_hidden()
                assert len(submissions) == before_recovery, "Recovery must not call Start again"
                fail_stop[0] = True
                page.locator("#outcome-stop-btn").click()
                expect(page.locator("#toast-stack")).to_contain_text("Stop not confirmed")
                expect(page.locator("#outcome-start-btn")).to_be_disabled()
                before = len(submissions)
                state.update(running=False, phase="failed", error="ALO rejected", cleanup_error="Account unavailable", cleanup_confirmed=False, can_start=False)
                page.evaluate("value => window.__testSocket.onmessage({data: JSON.stringify(value)})", state)
                expect(page.locator("#outcome-form-alert")).to_contain_text("ALO rejected")
                expect(page.locator("#outcome-run-banner")).to_contain_text("arrêt non confirmé")
                expect(page.locator("#outcome-start-btn")).to_be_disabled()
                expect(page.locator("#sessions-reconcile-btn")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Error state overflows viewport"
                page.locator("#toast-stack").evaluate("el => el.replaceChildren()")
                page.screenshot(path=str(Path(tempfile.gettempdir()) / f"zeus-stop-unconfirmed-{width}.png"), full_page=True)
                page.locator("#sessions-reconcile-btn").click()
                expect(page.locator("#outcome-start-btn")).to_be_disabled()
                state.update(cleanup_confirmed=True, can_start=True, cleanup_error="")
                page.evaluate("value => window.__testSocket.onmessage({data: JSON.stringify(value)})", state)
                expect(page.locator("#outcome-start-btn")).to_be_enabled()
                expect(page.locator("#outcome-form-alert")).to_contain_text("ALO rejected")
                assert len(submissions) == before, "Automatic restart after recovery"
                assert not errors, errors
                print(f"PASS {width}px: keyboard, conditional fields, failure recovery, payloads, active mode, reload, stop; {screenshot}")
                context.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
