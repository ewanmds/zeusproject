"""Mocked browser QA; all HTTP and WebSocket traffic stays inside this test."""
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]


def main():
    sent, errors = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script("""
            localStorage.setItem('zeus_access_code', 'offline-test');
            window.WebSocket=class{static OPEN=1;static CONNECTING=0;readyState=1;close(){}send(){}};
        """)
        def route(call):
            url = urlparse(call.request.url)
            if url.hostname != 'zeus.test':
                call.abort()
                return
            if url.path.startswith('/api/'):
                if url.path == '/api/grid/start':
                    sent.append(call.request.post_data_json)
                    result = {'status': 'starting'}
                elif url.path == '/api/grid/tokens':
                    result = {'tokens': [{'key': 'SP500', 'symbol': 'xyz:SP500'}, {'key': 'XYZ100', 'symbol': 'xyz:XYZ100'}]}
                elif url.path == '/api/grid/status':
                    result = dict(type='grid_stats', running=False, bots=[dict(token='SP500', strategy='freqtrade',
                        phase='failed', running=False, position_known=False, position_usd=None,
                        error='Arrêt non confirmé', fills=0, volume_usd=0)])
                else:
                    result = dict(running=False, bots=[])
                call.fulfill(json=result)
                return
            name = 'index.html' if url.path == '/' else url.path.lstrip('/')
            if name not in {'index.html', 'index.css', 'app.js', 'config.js'}:
                call.abort()
                return
            mime = {'html': 'text/html', 'js': 'application/javascript', 'css': 'text/css'}[name.rsplit('.', 1)[1]]
            call.fulfill(body=(ROOT / 'frontend' / name).read_bytes(), content_type=mime)
        page.route('**/*', route)
        page.goto('https://zeus.test/')
        page.get_by_label('Mode de cotation').select_option('freqtrade')
        assert page.locator('#grid-heading').inner_text() == 'Freqtrade'
        assert page.locator('#grid-offset-field').is_hidden()
        page.wait_for_selector('[data-f="title"]')
        assert 'FREQTRADE' in page.locator('[data-f="title"]').inner_text()
        assert page.locator('[data-f="pos"]').inner_text() == '—'
        page.locator('#grid-order-size').fill('40')
        page.locator('#grid-start-btn').click()
        page.wait_for_timeout(100)
        assert sent == [dict(token='SP500', strategy='freqtrade', mode='live', order_size_usd=40)], sent
        folder = ROOT / 'integrations' / 'freqtrade' / '.build'
        folder.mkdir(exist_ok=True)
        page.screenshot(path=str(folder / 'ui-desktop.png'), full_page=True)
        page.get_by_label('Mode de cotation').select_option('mid')
        assert page.locator('#grid-offset-field').is_visible()
        page.get_by_label('Mode de cotation').select_option('bbo')
        assert page.locator('#grid-offset-field').is_hidden()
        page.get_by_label('Mode de cotation').select_option('freqtrade')
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(folder / 'ui-mobile.png'), full_page=True)
        assert not errors, errors
        browser.close()
        print('UI OK: Freqtrade/BBO/MID, mocked live payload, unknown position, desktop/mobile, no JS errors.')


if __name__ == '__main__':
    main()
