"""Offline UI checks. Requires Playwright + Chromium; never contacts a trading API.

Run: python scripts/verify_ui.py [--screenshots DIRECTORY]
"""
import argparse
import functools
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    handler = functools.partial(QuietHandler, directory=str(root / "frontend"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bot = dict(token="SP500", strategy="mid", offset_bps=7.5, running=True,
               phase="running", volume_usd=10000, fees=1.2, realized_pnl=0.8,
               net_pnl=-0.4, cost_per_1m=40, fills=12, fills_per_hour=24,
               order_placements=15, order_modifications=3, order_cancellations=2,
               order_actions_per_hour=40, mid_price=5800, position_usd=0,
               spread_bps=1.5, duration="00:30:00", action="Quotes actives")
    grid = dict(type="grid_stats", running=False, bots=[])
    requests = []
    sockets = []
    errors = []
    protected = False

    def route_api(route):
        request = route.request
        path = request.url.split("/api/", 1)[1]
        data, status = {}, 200
        if protected and request.headers.get("x-api-token") != "test-only":
            status = 401
        elif path == "grid/tokens":
            data = {"tokens": [{"key": "SP500", "symbol": "xyz:SP500"},
                                 {"key": "GOLD", "symbol": "xyz:GOLD"}]}
        elif path == "grid/status":
            data = grid
        elif path == "worldcup/status":
            data = {"type": "worldcup_stats", "running": False}
        elif request.method == "POST":
            requests.append((path, request.post_data_json))
            status, data = 400, {"message": "Simulated persistent error for UI verification"}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(
                (f"http://127.0.0.1:{server.server_port}/", "https://fonts.googleapis.com/", "https://fonts.gstatic.com/")) else route.abort())
            context.route("**/api/**", route_api)
            context.route_web_socket("**/ws*", lambda socket: sockets.append(socket))
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.evaluate("document.fonts.ready")
            page.wait_for_function("document.querySelector('#session-count').textContent.includes('total')")
            assert page.locator("#grid-empty-state").is_visible()
            assert page.locator("#grid-strategy option").evaluate_all("options => options.map(option => option.value)") == ["bbo", "mid"]
            assert page.locator("#overview-volume").inner_text() == "\u2014"
            palette = page.evaluate("['--bg','--primary','--cta'].map(x=>getComputedStyle(document.documentElement).getPropertyValue(x).trim())")
            assert palette == ["#090d16", "#f59e0b", "#8b5cf6"], palette
            for width in [1440, 768, 390, 320]:
                page.set_viewport_size({"width": width, "height": 1000})
                page.evaluate("window.scrollTo({top:0, behavior:'instant'})")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                if args.screenshots:
                    page.screenshot(path=str(args.screenshots / f"empty-{width}.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator("#grid-token-trigger").click()
            page.locator("#grid-token-search").press("ArrowDown")
            page.keyboard.press("End")
            assert page.locator("#grid-token-options .token-option").last.evaluate("el=>el===document.activeElement")
            page.keyboard.press("Escape")
            assert page.locator("#grid-token-trigger").evaluate("el=>el===document.activeElement")
            page.locator("#tab-grid").focus()
            page.keyboard.press("ArrowRight")
            assert page.locator("#page-worldcup").is_visible()
            page.locator("#wc-market-trigger").click()
            page.locator("#wc-market-search").press("ArrowDown")
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            assert page.locator("#wc-market-trigger").evaluate("el=>el===document.activeElement")
            page.locator("#wc-spread").fill("")
            page.locator("#wc-start-btn").click()
            assert page.locator("#wc-spread").get_attribute("aria-invalid") == "true"
            assert not requests
            page.locator("#tab-grid").click()
            page.locator("#grid-order-size").fill("")
            page.locator("#grid-start-btn").click()
            assert page.locator("#grid-order-size").get_attribute("aria-invalid") == "true"
            assert not requests
            page.locator("#grid-order-size").fill("100")
            page.locator("#grid-strategy").select_option("mid")
            page.locator("#grid-offset-bps").fill("7.5")
            page.locator("#grid-start-btn").click()
            page.locator("#grid-form-alert").wait_for(state="visible")
            # Inspect the real notification entrance at 10% playback speed.
            page.evaluate("document.getAnimations().forEach(animation => { animation.playbackRate = 0.1; })")
            assert requests[-1] == ("grid/start", dict(token="SP500", strategy="mid", mode="live", order_size_usd=100, offset_bps=7.5))
            assert not page.locator("#grid-start-btn").is_disabled()
            grid.update(running=True, bots=[bot])
            sockets[-1].send(json.dumps(grid))
            page.locator(".bot-card").wait_for()
            assert page.locator("#overview-cost").inner_text() == "$40.00"
            assert page.locator("#overview-pnl").inner_text() == "-$0.40"
            page.locator(".bot-card-stop").focus()
            page.locator(".bot-card").evaluate("el=>el.dataset.retained='yes'")
            bot.update(phase="connecting", offset_bps=8, error="A complete actionable error message " * 5)
            sockets[-1].send(json.dumps(grid))
            page.wait_for_function("document.querySelector('.bot-card').classList.contains('failed')")
            assert page.locator(".bot-card").get_attribute("data-retained") == "yes"
            assert page.locator(".bot-card-stop").evaluate("el=>el===document.activeElement")
            for width in [1440, 768, 390, 320]:
                page.set_viewport_size({"width": width, "height": 1000})
                page.evaluate("window.scrollTo({top:0, behavior:'instant'})")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                if args.screenshots:
                    page.screenshot(path=str(args.screenshots / f"session-{width}.png"), full_page=True)
            page.locator(".bot-card-stop").focus()
            bot["running"] = False
            grid["running"] = False
            sockets[-1].send(json.dumps(grid))
            page.wait_for_function("document.querySelector('.bot-card').classList.contains('stopped')")
            assert page.locator(".bot-card").evaluate("el=>el===document.activeElement")
            assert page.locator(".bot-card-stop").is_hidden()
            assert page.locator("#overview-cost").inner_text() == "\u2014"
            assert page.locator(".bot-card-action").is_visible()
            page.emulate_media(reduced_motion="reduce")
            assert page.locator(".page.active").evaluate("el=>getComputedStyle(el).animationName") == "none"
            assert page.locator(".btn").first.evaluate("el=>getComputedStyle(el).transitionDuration") == "0s"
            page.set_viewport_size({"width": 390, "height": 844})
            protected = True
            page.reload()
            page.wait_for_function("document.querySelector('#access-gate').open")
            assert page.locator("#access-code-input").evaluate("el=>el===document.activeElement")
            page.keyboard.press("Shift+Tab")
            assert page.locator("#access-code-submit").evaluate("el=>el===document.activeElement")
            page.keyboard.press("Tab")
            assert page.locator("#access-code-input").evaluate("el=>el===document.activeElement")
            page.keyboard.press("Escape")
            assert page.locator("#access-gate").evaluate("el=>el.open")
            if args.screenshots:
                page.screenshot(path=str(args.screenshots / "access-390.png"))
            page.locator("#access-code-input").fill("test-only")
            page.locator("#access-code-submit").click()
            page.wait_for_function("!document.querySelector('#access-gate').open")
            assert not errors, errors
            browser.close()
            print("PASS: original palette; 320/390/768/1440 layouts; both keyboard pickers; tabs; validation; mocked MID payload; persistent error; live card focus; CPM; reduced motion; modal focus and authentication. No real trading API called.")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
