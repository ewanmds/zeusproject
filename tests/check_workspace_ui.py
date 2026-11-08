"""Complete browser flow against real auth with disposable wallets and fake execution."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import requests
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
                            if(method==='eth_signTypedData_v4')return window.__signTyped(params[1]);
                            if(method==='wallet_switchEthereumChain'){chain=params[0].chainId;return null}
                            throw Error('Unexpected test wallet action: '+method);
                        },on:(name,fn)=>{callbacks[name]=fn},removeListener:(name)=>{delete callbacks[name]}};
                        window.__switchWallet=(address)=>{window.__walletAddress=address;callbacks.accountsChanged?.([address])};
                        window.ethereum=provider;
                        window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',
                            {detail:{info:{uuid:'offline-test-wallet',name:'Test Wallet',rdns:'test.wallet'},provider}})));
                    })()''')
                    def route(call):
                        if call.request.url.startswith(origin+'/api/v2/markets'):
                            call.fulfill(json={'tokens':[{'key':'IO_SNDK','symbol':'io:SNDK'},{'key':'SP500','symbol':'xyz:SP500'}]});return
                        if call.request.url.startswith(origin+'/api/v2/scanner'):
                            call.fulfill(json={'ready':True,'candidates':[{'market':'#101','name':'Test market','queue_proxy_sec':20,'spread_bps':.2,'volume_usd_1m':550}]});return
                        if not call.request.url.startswith(origin):call.abort();return
                        call.continue_()
                    page.route('**/*',route)
                    page.goto(origin);page.evaluate('(value)=>window.__walletAddress=value',a.address)
                    expect(page.get_by_role('heading',name='Lancer, simplement.')).to_be_visible()
                    page.screenshot(path=str(folder/f'run-disconnected-{width}.png'),full_page=True)
                    overflow=page.evaluate('''()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,
                        elements:[...document.querySelectorAll('body *')].filter(el=>el.getBoundingClientRect().right>innerWidth+1||el.scrollWidth>el.clientWidth+2)
                        .slice(0,20).map(el=>({tag:el.tagName,cls:el.className,right:el.getBoundingClientRect().right,scroll:el.scrollWidth,client:el.clientWidth}))})''')
                    assert overflow['scroll']<=overflow['width'],overflow
                    page.get_by_role('button',name='Connecter',exact=True).click()
                    page.get_by_role('button',name='Test Wallet').click()
                    expect(page.get_by_role('dialog')).not_to_be_visible()
                    # Real SIWE cookie, CSRF and encrypted agent preparation/approval.
                    page.get_by_role('button',name='Vérifier et lancer',exact=True).count()
                    page.get_by_role('button',name='Autoriser le trading',exact=True).click()
                    expect(page.get_by_role('heading',name='Votre compte.')).to_be_visible()
                    page.get_by_role('button',name='Autoriser le trading',exact=True).click()
                    expect(page.get_by_role('button',name='Préparer une session',exact=True)).to_be_visible()
                    page.screenshot(path=str(folder/f'account-ready-{width}.png'),full_page=True)
                    page.get_by_role('button',name='Préparer une session',exact=True).click()
                    page.get_by_text('Options de session',exact=True).click()
                    page.get_by_role('button',name='Durée',exact=True).click()
                    page.get_by_label('Durée · minutes').fill('15')
                    page.get_by_label('Seuil de perte réalisée · USD').fill('8')
                    page.get_by_role('button',name='Vérifier et lancer',exact=True).click()
                    expect(page.get_by_role('dialog')).to_be_visible()
                    expect(page.get_by_role('dialog')).to_contain_text('00:15:00')
                    expect(page.get_by_role('dialog')).to_contain_text('$8.00')
                    page.screenshot(path=str(folder/f'review-{width}.png'),full_page=True)
                    page.get_by_role('dialog').get_by_role('button',name='Lancer la session',exact=True).click()
                    expect(page.get_by_role('heading',name='Vos sessions.')).to_be_visible()
                    card=page.locator('.session-card');expect(card).to_have_count(1)
                    expect(card).to_contain_text('$25.00')
                    page.screenshot(path=str(folder/f'sessions-{width}.png'),full_page=True)
                    original=card.element_handle()
                    manager=app.state.runtimes.manager(a.address.lower());manager.rows[0].update(volume_usd=75,fills=3)
                    app.state.runtimes.invalidate(a.address.lower())
                    expect(card).to_contain_text('$75.00')
                    assert original.evaluate('(el)=>el===document.querySelector(".session-card")')
                    # Details, keyboard closing, per-tenant Performance.
                    card.get_by_role('button',name='Détails').click()
                    expect(page.get_by_role('dialog')).to_contain_text('$75.00')
                    manager.rows[0].update(volume_usd=100,fills=4);app.state.runtimes.invalidate(a.address.lower())
                    expect(page.get_by_role('dialog')).to_contain_text('$100.00')
                    page.keyboard.press('Escape');expect(page.get_by_role('dialog')).not_to_be_visible()
                    page.get_by_role('link',name='Performances',exact=True).click()
                    expect(page.get_by_role('heading',name='Une vue claire.')).to_be_visible()
                    page.screenshot(path=str(folder/f'performance-{width}.png'),full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.get_by_role('link',name='Sessions',exact=False).click()
                    card.get_by_role('button',name='Arrêter',exact=True).click()
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
            finally:browser.close()
    finally:
        server.should_exit=True;thread.join(10);app.state.store.engine.dispose();temporary.cleanup()


if __name__=='__main__':main()
