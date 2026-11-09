"""Complete browser flow against real auth with disposable wallets and fake execution."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import uvicorn
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from zeus.workspace.app import create_app, Settings
from test_workspace import FakeAccounts, FakeManager


def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    temporary=tempfile.TemporaryDirectory()
    app=create_app(Settings(Path(temporary.name),origin=origin,warm_catalogs=False),
                   accounts_factory=FakeAccounts,manager_factory=FakeManager)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    until=time.time()+15
    while not server.started:
        if time.time()>until:raise AssertionError('Test server unavailable')
        time.sleep(.05)
    folder=ROOT/'output/playwright/workspace';folder.mkdir(parents=True,exist_ok=True)
    seen=set()
    def record(page):
        seen.update(page.evaluate("""()=>[...document.querySelectorAll('[data-layro]')].filter(el=>el.getClientRects().length && getComputedStyle(el).visibility!=='hidden' && !el.closest('[hidden],[aria-hidden=true]')).map(el=>el.dataset.layro)"""))
    def capture(page, name):
        record(page)
        page.evaluate('window.scrollTo(0,0)')
        page.screenshot(path=str(folder/name),full_page=True)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch()
            try:
                for width in (1440,390):
                    a,b=Account.create(),Account.create();selected=[a]
                    context=browser.new_context(viewport={'width':width,'height':1000},reduced_motion='reduce')
                    page=context.new_page();errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.expose_function('__signMessage',lambda raw:'0x'+Account.sign_message(encode_defunct(hexstr=raw),selected[0].key).signature.hex())
                    page.expose_function('__signTyped',lambda raw:'0x'+Account.sign_message(encode_typed_data(full_message=json.loads(raw)),selected[0].key).signature.hex())
                    page.add_init_script('''(() => {
                        let chain='0x1';const callbacks={};
                        const provider={request:async ({method,params})=>{
                            if(method==='eth_requestAccounts'||method==='eth_accounts')return [window.__walletAddress];
                            if(method==='eth_chainId')return chain;
                            if(method==='personal_sign')return window.__signMessage(params[0]);
                            if(method==='eth_signTypedData_v4'){if(window.__rejectAgent){window.__rejectAgent=false;throw Object.assign(new Error('Refus de test'),{code:4001})}return window.__signTyped(params[1])}
                            if(method==='wallet_switchEthereumChain'){chain=params[0].chainId;return null}
                            throw Error('Unexpected test wallet action: '+method);
                        },on:(name,fn)=>{callbacks[name]=fn},removeListener:(name)=>{delete callbacks[name]}};
                        window.__switchWallet=(address)=>{window.__walletAddress=address;callbacks.accountsChanged?.([address])};
                        window.ethereum=provider;
                        window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',
                            {detail:{info:{uuid:'offline-test-wallet',name:'Test Wallet',rdns:'test.wallet'},provider}})));
                    })()''')
                    account_gate={'hold':width==1440,'routes':[]}
                    lost_launch={'once':width==1440}
                    uncertain_stop={'once':width==1440}
                    preview_gate={'hold':False,'route':None}
                    def route(call):
                        if call.request.url==origin+'/api/v2/account' and account_gate['hold']:
                            account_gate['routes'].append(call);return
                        if call.request.url==origin+'/api/v2/sessions' and call.request.method=='POST' and lost_launch['once']:
                            lost_launch['once']=False;call.fetch();call.abort();return
                        if call.request.url.endswith('/stop') and uncertain_stop['once']:
                            uncertain_stop['once']=False;call.fulfill(status=503,json={'detail':'Arrêt non confirmé. Réessayez la vérification.'});return
                        if call.request.url==origin+'/api/v2/sessions/preview' and preview_gate['hold']:
                            preview_gate['route']=call;return
                        if call.request.url.startswith(origin+'/api/v2/markets'):
                            call.fulfill(json={'tokens':[{'key':'IO_SNDK','symbol':'io:SNDK'},{'key':'SP500','symbol':'xyz:SP500'}]});return
                        if call.request.url.startswith(origin+'/api/v2/scanner'):
                            call.fulfill(json={'ready':True,'candidates':[{'market':'#101','name':'Test market','queue_proxy_sec':20,'spread_bps':.2,'volume_usd_1m':550}]});return
                        if not call.request.url.startswith(origin):call.abort();return
                        call.continue_()
                    page.route('**/*',route)
                    page.goto(origin);page.evaluate('(value)=>window.__walletAddress=value',a.address)
                    expect(page.get_by_role('heading',name='Lancer, simplement.')).to_be_visible()
                    side=page.get_by_role('group',name='Côté Outcome',exact=True)
                    expect(side.get_by_role('button',name='No',exact=True)).to_have_attribute('aria-pressed','true')
                    side.get_by_role('button',name='No',exact=True).focus()
                    page.keyboard.press('ArrowLeft')
                    expect(side.get_by_role('button',name='Yes',exact=True)).to_be_focused()
                    expect(side.get_by_role('button',name='Yes',exact=True)).to_have_attribute('aria-pressed','true')
                    page.keyboard.press('End')
                    expect(side.get_by_role('button',name='No',exact=True)).to_be_focused()
                    expect(side.get_by_role('button',name='No',exact=True)).to_have_attribute('tabindex','0')
                    expect(side.get_by_role('button',name='Yes',exact=True)).to_have_attribute('tabindex','-1')
                    alignment=side.evaluate("""el=>{
                        const active=el.querySelector('[aria-pressed=true]').getBoundingClientRect();
                        const thumb=el.querySelector('.segment-thumb').getBoundingClientRect();
                        return {left:Math.abs(active.left-thumb.left),width:Math.abs(active.width-thumb.width)};
                    }""")
                    assert alignment['left']<1.5 and alignment['width']<1.5,alignment
                    assert side.locator('.segment-thumb').evaluate('el=>getComputedStyle(el).transitionDuration')=='0s'
                    if width==390:
                        assert side.get_by_role('button',name='No',exact=True).bounding_box()['height']>=44
                    page.evaluate('document.activeElement.blur(); window.scrollTo(0,0)')
                    capture(page, f'run-disconnected-{width}.png')
                    overflow=page.evaluate('''()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,
                        elements:[...document.querySelectorAll('body *')].filter(el=>el.getBoundingClientRect().right>innerWidth+1||el.scrollWidth>el.clientWidth+2)
                        .slice(0,20).map(el=>({tag:el.tagName,cls:el.className,right:el.getBoundingClientRect().right,scroll:el.scrollWidth,client:el.clientWidth}))})''')
                    assert overflow['scroll']<=overflow['width'],overflow
                    # Responsive layout and safe global search without a wallet.
                    if width==1440:
                        for check_width,check_height in ((360,800),(768,1024),(1280,720),(1920,1080),(640,360)):
                            page.set_viewport_size({'width':check_width,'height':check_height})
                            try:page.wait_for_function('document.documentElement.scrollWidth<=innerWidth',timeout=2500)
                            except Exception:pass
                            overflow=page.evaluate("""()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,elements:[...document.querySelectorAll('body *')].filter(el=>el.getBoundingClientRect().right>innerWidth+1 && getComputedStyle(el).position!=='fixed').slice(0,12).map(el=>({tag:el.tagName,cls:el.className,right:el.getBoundingClientRect().right,width:el.getBoundingClientRect().width}))})""")
                            if overflow['scroll']>check_width:page.screenshot(path=str(folder/f'overflow-{check_width}.png'),full_page=True)
                            assert overflow['scroll']<=check_width,overflow
                        page.set_viewport_size({'width':width,'height':1000})
                    page.get_by_role('button',name='Ouvrir la recherche',exact=True).click()
                    query=page.get_by_role('combobox',name='Recherche globale',exact=True)
                    query.fill('zzzz-no-result');expect(page.get_by_text('Aucun résultat.',exact=True)).to_be_visible();record(page)
                    query.fill('Sessions');page.keyboard.press('Enter')
                    expect(page.get_by_role('heading',name='Vos sessions.')).to_be_visible();record(page)
                    page.get_by_role('link',name='Lancer',exact=True).click()
                    page.get_by_role('button',name='Connecter',exact=True).click()
                    page.get_by_role('button',name='Test Wallet').click()
                    expect(page.get_by_role('dialog')).not_to_be_visible()
                    # Real SIWE cookie, CSRF and encrypted agent preparation/approval.
                    page.get_by_role('button',name='Vérifier et lancer',exact=True).count()
                    page.get_by_role('button',name='Autoriser le trading',exact=True).click()
                    expect(page.get_by_role('heading',name='Votre compte.')).to_be_visible()
                    if width==1440:
                        expect(page.locator('[data-layro="preetsuthar17/skeleton"]')).to_be_visible();record(page)
                        account_gate['hold']=False
                        for held in account_gate['routes']:held.continue_()
                        expect(page.locator('.wallet-balance')).to_contain_text('$100.00')
                    page.evaluate('window.__rejectAgent=true')
                    page.get_by_role('button',name='Autoriser le trading',exact=True).click()
                    expect(page.get_by_role('alert')).to_contain_text('Autorisation refusée');record(page)
                    page.get_by_role('button',name='Autoriser le trading',exact=True).click()
                    expect(page.get_by_role('button',name='Préparer une session',exact=True)).to_be_visible()
                    page.get_by_role('button',name='Identité du compte',exact=True).click()
                    expect(page.locator('.rich-popover')).to_be_visible();record(page);page.keyboard.press('Escape')
                    page.get_by_role('button',name='Masquer le solde',exact=True).click()
                    expect(page.locator('.wallet-balance')).not_to_contain_text('$100.00')
                    page.get_by_role('button',name='Afficher le solde',exact=True).click()
                    page.get_by_role('button',name='Menu du wallet',exact=False).click()
                    expect(page.get_by_role('menuitem',name='Votre compte',exact=True)).to_be_visible();record(page);page.keyboard.press('Escape')
                    capture(page, f'account-ready-{width}.png')
                    page.get_by_role('button',name='Préparer une session',exact=True).click()
                    page.locator('.strategy-card').filter(has_text='BBO').click()
                    market=page.get_by_role('combobox',name='Marché HIP-3',exact=True);market.click()
                    page.get_by_role('option',name='io:SNDK',exact=True).click();expect(market).to_contain_text('io:SNDK');record(page)
                    page.get_by_role('textbox',name='Rechercher un marché',exact=True).fill('SP500')
                    expect(market).to_contain_text('io:SNDK')
                    page.get_by_role('textbox',name='Rechercher un marché',exact=True).fill('')
                    page.locator('.strategy-card').filter(has_text='Auto Outcome').click()
                    page.get_by_text('Options de session',exact=True).click()
                    page.get_by_role('button',name='Durée',exact=True).click()
                    page.get_by_label('Durée · minutes').fill('15')
                    page.get_by_label('Seuil de perte réalisée · USD').fill('8')
                    record(page)
                    if width==390:
                        page.get_by_role('button',name='Terminer les réglages',exact=True).click()
                    preview_button=page.get_by_role('button',name='Vérifier et lancer',exact=True)
                    before_width=preview_button.bounding_box()['width']
                    preview_gate['hold']=True
                    with page.expect_request(lambda request:request.url==origin+'/api/v2/sessions/preview'):
                        preview_button.click()
                    loading=page.get_by_role('button',name='Vérification',exact=True)
                    expect(loading).to_have_attribute('aria-busy','true')
                    expect(loading).to_be_disabled()
                    assert abs(loading.bounding_box()['width']-before_width)<1
                    assert preview_gate['route'] is not None
                    record(page)
                    preview_gate['hold']=False;preview_gate['route'].fulfill(status=503,json={'detail':'Vérification de test indisponible.'})
                    expect(page.get_by_role('alert')).to_contain_text('Vérification de test indisponible.');record(page)
                    page.get_by_role('button',name='Vérifier et lancer',exact=True).click()
                    expect(page.get_by_role('dialog')).to_be_visible()
                    expect(page.get_by_role('dialog')).to_contain_text('00:15:00')
                    expect(page.get_by_role('dialog')).to_contain_text('$8.00')
                    capture(page, f'review-{width}.png')
                    page.get_by_role('dialog').get_by_role('button',name='Lancer la session',exact=True).click()
                    if width==1440:
                        expect(page.get_by_text('Lancement à vérifier',exact=True)).to_be_visible()
                        page.get_by_role('button',name='Vérifier',exact=True).click()
                    expect(page.get_by_role('heading',name='Vos sessions.')).to_be_visible()
                    card=page.locator('.session-card');expect(card).to_have_count(1)
                    expect(card).to_contain_text('$25.00')
                    capture(page, f'sessions-{width}.png')
                    record(page)
                    page.get_by_role('button',name='Coût par million',exact=True).click()
                    expect(page.locator('.rich-popover')).to_be_visible();record(page);page.keyboard.press('Escape')
                    toggle=page.get_by_role('switch',name='Vue compacte',exact=True);toggle.click()
                    expect(page.locator('.compact-sessions')).to_contain_text('$25.00');record(page)
                    capture(page,f'compact-sessions-{width}.png');toggle.click()
                    original=card.element_handle()
                    manager=app.state.runtimes.manager(a.address.lower());manager.rows[0].update(volume_usd=75,fills=3)
                    app.state.runtimes.invalidate(a.address.lower())
                    expect(card).to_contain_text('$75.00')
                    assert original.evaluate('(el)=>el===document.querySelector(".session-card")')
                    # Stale rows stay visible but cannot look like confirmed live execution.
                    manager.rows[0].update(stale=True,market_name='NBA Regular Season: Dallas Mavericks v Indiana Pacers — résultat du match')
                    app.state.runtimes.invalidate(a.address.lower())
                    expect(card.locator('.zeus-status')).to_contain_text('Données retardées')
                    assert 'is-running' not in card.get_attribute('class').split()
                    expect(card.locator('.session-metrics .positive')).to_have_count(0)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    capture(page, f'sessions-stale-{width}.png')
                    manager.rows[0].update(stale=False)
                    app.state.runtimes.invalidate(a.address.lower())
                    expect(card).to_contain_text('$75.00')
                    # Details, keyboard closing, per-tenant Performance.
                    card.get_by_role('button',name='Détails').click()
                    expect(page.get_by_role('dialog')).to_contain_text('$75.00')
                    page.get_by_role('tab',name='Position',exact=True).click()
                    expect(page.get_by_role('tabpanel')).to_contain_text('Position valorisée')
                    page.get_by_role('tab',name='Paramètres',exact=True).click()
                    expect(page.get_by_role('tabpanel')).to_contain_text('$25.00');record(page)
                    page.get_by_role('tab',name='Exécution',exact=True).click()
                    manager.rows[0].update(volume_usd=100,fills=4);app.state.runtimes.invalidate(a.address.lower())
                    expect(page.get_by_role('dialog')).to_contain_text('$100.00')
                    page.keyboard.press('Escape');expect(page.get_by_role('dialog')).not_to_be_visible()
                    page.get_by_role('link',name='Performances',exact=True).click()
                    expect(page.get_by_role('heading',name='Une vue claire.')).to_be_visible()
                    expect(page.locator('[data-layro="legions-developer/recharts-bar-chart"]')).to_be_visible()
                    expect(page.locator('[data-layro="legions-developer/recharts-pie-chart"] svg')).to_be_visible()
                    record(page)
                    capture(page, f'performance-{width}.png')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.get_by_role('link',name='Sessions',exact=False).click()
                    card.get_by_role('button',name='Arrêter',exact=True).click()
                    record(page)
                    page.get_by_role('button',name='Confirmer l’arrêt',exact=True).click()
                    if width==1440:
                        expect(page.get_by_role('alertdialog')).to_contain_text('Arrêt non confirmé.');record(page)
                        page.get_by_role('button',name='Confirmer l’arrêt',exact=True).click()
                    expect(card).to_have_count(0)
                    page.get_by_role('button',name='Terminées',exact=True).click();expect(card).to_have_count(1)
                    # Change wallet: clear A's live rows and caches immediately.
                    selected[0]=b;page.evaluate('(value)=>window.__switchWallet(value)',b.address)
                    expect(page.get_by_role('button',name='Connecter',exact=True)).to_be_visible()
                    expect(page.locator('.session-card')).to_have_count(0)
                    page.get_by_role('button',name='Connecter',exact=True).click()
                    page.get_by_role('button',name='Test Wallet').click()
                    expect(page.get_by_role('dialog')).not_to_be_visible()
                    expect(page.locator('.session-card')).to_have_count(0)
                    assert not errors,errors
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    print(f'PASS {width}px: real signed login, agent signature, launch, live fills, keyed cards, stop, wallet isolation, no overflow')
                    context.close()
                manifest=json.loads((ROOT/'frontend/workspace/layro-components.json').read_text('utf-8'))['components']
                expected={item['id'] for item in manifest}
                missing=expected-seen
                (folder/'layro-coverage.json').write_text(json.dumps({'expected':len(expected),'observed':len(expected&seen),'missing':sorted(missing),'sources':sorted(seen)},indent=2),encoding='utf-8')
                assert not missing,sorted(missing)
                print(f'PASS Layro: {len(expected)} distinct source-backed components observed in real UI states')
            finally:browser.close()
    finally:
        server.should_exit=True;thread.join(10);app.state.store.engine.dispose();temporary.cleanup()


if __name__=='__main__':main()
