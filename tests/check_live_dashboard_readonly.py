"""Read-only smoke test against the running dashboard; never starts/stops a bot."""
import json
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = dotenv_values(ROOT/'.env')
    base = 'http://127.0.0.1:'+str(env.get('BOT_PORT', '8080'))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1100})
            page.add_init_script('localStorage.setItem("zeus_access_code",'+json.dumps(env.get('BOT_API_TOKEN', ''))+');')
            errors, frames, render_errors = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: render_errors.append(message.text)
                    if message.type == 'error' and message.text.startswith('Dashboard render failed') else None)
            def socket(ws):
                def received(raw):
                    frame = json.loads(raw)
                    if frame.get('type') == 'worldcup_stats': frames.append(frame)
                ws.on('framereceived', received)
            page.on('websocket', socket)
            def route(call):
                url = urlparse(call.request.url)
                if url.hostname not in ('127.0.0.1', 'localhost'):
                    call.abort(); return
                if call.request.method != 'GET':
                    raise AssertionError('Live verification attempted a mutation')
                call.continue_()
            page.route('**/*', route)
            page.goto(base+'/', wait_until='domcontentloaded')
            page.locator('#tab-outcome').click()
            page.wait_for_function('document.querySelector("#outcome-fills").textContent !== "0"')
            page.wait_for_function('document.querySelector("#connection-status .status-text").textContent === "Connected"')
            expect(page.locator('#outcome-run-banner strong')).to_have_text('1 active Outcome bot(s)')
            expect(page.locator('#data-freshness')).to_have_text('Updated just now')
            page.wait_for_timeout(1500)  # Verify a second real 1 Hz push, not only initial REST.
            assert not errors, errors
            assert not render_errors, render_errors
            assert len(frames) >= 2, 'No continuing real WebSocket Outcome stream'
            folder = ROOT/'artifacts'/'dashboard-recovery'; folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder/'live-readonly.png'), full_page=True)
            result = {'overview_volume': page.locator('#outcome-volume').inner_text(),
                      'overview_fills': page.locator('#outcome-fills').inner_text(),
                      'card_volume': page.locator('#outcome-bots-list [data-f="volume_usd"]').all_inner_texts(),
                      'card_fills': page.locator('#outcome-bots-list [data-f="fills"]').all_inner_texts(),
                      'websocket_frames': len(frames), 'javascript_errors': errors,
                      'freshness': page.locator('#data-freshness').inner_text()}
            assert result['card_volume'] == [result['overview_volume']], result
            assert result['card_fills'] == [result['overview_fills']], result
            (folder/'live-readonly.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result))
            print('PASS live dashboard: real WebSocket, nonzero fills, GET-only browser')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
