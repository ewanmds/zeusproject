"""Browser QA for quote visibility and coalesced live updates; all APIs mocked."""
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for width in (1440, 390):
                page = browser.new_page(viewport={'width': width, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.add_init_script('''localStorage.setItem('zeus_access_code','offline');
                    window.WebSocket=class {static OPEN=1;static CONNECTING=0;readyState=1;
                    constructor(){window.__testSocket=this;}close(){}send(){}};''')
                bot = dict(session_id='auto', market='#257961', market_name='Napoli', running=True,
                    phase='running', cleanup_confirmed=False, position_known=True, position_usd=0,
                    accounting_complete=True, volume_usd=0, fees=0, net_pnl=0, fills=0, duration='00:00:30',
                    execution_state='Bid working', account_sync_ms=81, orders_sent=1, cancels_sent=0,
                    quotes=[dict(bid_price=.34833, bid_size=72, ask_price=None, ask_size=0)],
                    config=dict(selection_mode='auto', exec_mode='maker', two_sided=True,
                                order_size_usd=25, max_position_usd=200))
                def status(revision=1):
                    return dict(type='worldcup_stats', source_id='offline', revision=revision,
                                bots=[bot], running=True, can_start=True, cleanup_confirmed=False)
                def route(call):
                    url = urlparse(call.request.url)
                    if url.hostname != 'zeus.test':
                        call.abort(); return
                    if call.request.method != 'GET':
                        raise AssertionError('Read-only browser QA attempted a mutation')
                    if url.path.startswith('/api/'):
                        data = {}
                        if url.path == '/api/outcomes/status': data = status()
                        elif url.path == '/api/grid/status': data = dict(type='grid_stats', running=False, bots=[])
                        elif url.path == '/api/grid/tokens': data = {'tokens': []}
                        elif url.path == '/api/outcomes/markets': data = {'outcomes': []}
                        call.fulfill(json=data); return
                    name = url.path.lstrip('/').removeprefix('frontend/') or 'index.html'
                    file = ROOT/'frontend'/name
                    if name == 'config.js': call.fulfill(body='window.ZEUS_CONFIG={};')
                    elif file.is_file(): call.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(name)[0] or 'text/plain')
                    else: call.fulfill(status=404, body='')
                page.route('**/*', route)
                page.goto('https://zeus.test/')
                page.locator('#tab-outcome').click()
                card = page.locator('#outcome-bots-list .bot-card')
                expect(card.locator('[data-f="state"]')).to_have_text('Bid working')
                expect(card.locator('[data-f="quotes"]')).to_have_text('BID 72 × 0.34833 · ASK —')
                page.evaluate('window.__originalCard=document.querySelector("#outcome-bots-list .bot-card")')
                bot.update(execution_state='Bid + Ask', fills=2,
                    quotes=[dict(bid_price=.34833, bid_size=72, ask_price=.34834, ask_size=72)])
                frames = []
                for revision in range(2, 34):
                    bot['volume_usd'] = revision
                    frames.append(json.loads(json.dumps(status(revision))))
                page.evaluate('frames=>frames.forEach(data=>window.__testSocket.onmessage({data:JSON.stringify(data)}))', frames)
                expect(card.locator('[data-f="state"]')).to_have_text('Bid + Ask')
                expect(card.locator('[data-f="quotes"]')).to_have_text('BID 72 × 0.34833 · ASK 72 × 0.34834')
                expect(page.locator('#outcome-volume')).to_have_text('$33.00')
                assert page.evaluate('window.__originalCard===document.querySelector("#outcome-bots-list .bot-card")')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert not errors, errors
                folder = ROOT/'artifacts'/'outcome-fast'
                folder.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(folder/f'quotes-{width}.png'), full_page=True)
                print(f'PASS {width}px: quote precision, execution state, 32 merged snapshots, stable DOM, no overflow')
                page.close()
        finally:
            browser.close()


if __name__ == '__main__':
    main()
