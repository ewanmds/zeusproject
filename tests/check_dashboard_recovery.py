"""Read-only browser regression: cached helpers, render failure and REST outage."""
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
                bot = dict(session_id='auto', market='#270441', market_name='Dallas - Indiana',
                    running=True, phase='running', cleanup_confirmed=False, position_known=True,
                    accounting_complete=True, position_usd=0, volume_usd=0, net_pnl=0, fees=0, fills=0,
                    duration='00:03:23', execution_state='Bid working',
                    quotes=[dict(bid_price=.42653, bid_size=59)],
                    config=dict(selection_mode='auto', exec_mode='maker', two_sided=True,
                                order_size_usd=25, max_position_usd=200))
                fail_reads = [False]
                def status(revision=1):
                    metrics = {key: bot[key] for key in ('volume_usd', 'net_pnl', 'fees', 'fills', 'duration')}
                    return dict(type='worldcup_stats', source_id='test', revision=revision,
                                display_bots=[bot], bots=[bot], active_metrics=dict(metrics,
                                running=True, active_markets=1, accounting_complete=True), running=True)
                def route(call):
                    url = urlparse(call.request.url)
                    if url.hostname != 'zeus.test': call.abort(); return
                    if call.request.method != 'GET': raise AssertionError('UI regression must stay read-only')
                    if url.path.startswith('/api/'):
                        if fail_reads[0] and url.path.endswith('/status'):
                            call.abort('failed'); return
                        data = {}
                        if url.path == '/api/outcomes/status': data = status()
                        elif url.path == '/api/grid/status': data = dict(type='grid_stats', running=False, bots=[])
                        elif url.path == '/api/grid/tokens': data = {'tokens': []}
                        elif url.path == '/api/outcomes/markets': data = {'outcomes': []}
                        call.fulfill(json=data); return
                    name = url.path.lstrip('/').removeprefix('frontend/') or 'index.html'
                    file = ROOT/'frontend'/name
                    if name == 'config.js': call.fulfill(body='window.ZEUS_CONFIG={};')
                    elif name == 'state.js':
                        # Realistic older helper cached under /frontend/state.js.
                        call.fulfill(body=file.read_text(encoding='utf-8')+'\ndelete window.ZeusState.quoteText; delete window.ZeusState.errorText;',
                                     content_type='application/javascript')
                    elif file.is_file(): call.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(name)[0] or 'text/plain')
                    else: call.fulfill(status=404, body='')
                page.route('**/*', route)
                page.goto('https://zeus.test/')
                page.locator('#tab-outcome').click()
                expect(page.locator('#outcome-fills')).to_have_text('0')
                bot.update(volume_usd=50.33113, fills=2, fees=.01932738, net_pnl=-.01873738)
                fail_reads[0] = True
                page.evaluate('data=>window.__testSocket.onmessage({data:JSON.stringify(data)})', status(2))
                expect(page.locator('#outcome-volume')).to_have_text('$50.33')
                expect(page.locator('#outcome-fills')).to_have_text('2')
                card = page.locator('#outcome-bots-list .bot-card')
                expect(card.locator('[data-f="volume_usd"]')).to_have_text('$50.33')
                expect(page.locator('#outcome-run-banner strong')).to_have_text('1 active Outcome bot(s)')
                expect(card.locator('[data-f="quotes"]')).to_contain_text('0.42653')
                assert not errors, errors
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                # One unrelated render error must not drop the Outcome frame.
                page.evaluate('''() => {
                    const card = document.querySelector('#outcome-bots-list .bot-card');
                    card.querySelector('[data-f="quotes"]').remove();
                }''')
                bot.update(volume_usd=100.66, fills=4)
                page.evaluate('data=>window.__testSocket.onmessage({data:JSON.stringify(data)})', status(3))
                expect(page.locator('#outcome-volume')).to_have_text('$100.66')
                expect(page.locator('#outcome-fills')).to_have_text('4')
                assert not errors, errors
                # A Grid render failure in the same frame cannot drop Outcome.
                bot.update(volume_usd=150.99, fills=6)
                page.evaluate('''data => {
                    const label=document.querySelector('#grid-run-duration');
                    Object.defineProperty(label,'textContent',{configurable:true,
                        get(){return 'injected'},set(){throw new Error('Offline Grid render failure')}});
                    window.__testSocket.onmessage({data:JSON.stringify({type:'grid_stats',running:false,bots:[],duration:'00:01:00'})});
                    window.__testSocket.onmessage({data:JSON.stringify(data)});
                }''', status(4))
                expect(page.locator('#outcome-volume')).to_have_text('$150.99')
                expect(page.locator('#outcome-fills')).to_have_text('6')
                expect(page.locator('#data-freshness')).to_have_text('Updated just now')
                page.evaluate("delete document.querySelector('#grid-run-duration').textContent")
                assert not errors, errors
                folder = ROOT/'artifacts'/'dashboard-recovery'; folder.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(folder/f'recovered-{width}.png'), full_page=True)
                print(f'PASS {width}px: cached helper, card repair, live financial counters despite REST failure')
                page.close()
        finally:
            browser.close()


if __name__ == '__main__':
    main()
