"""Offline browser regression: every request and WebSocket is mocked."""
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as p:
        browser=p.chromium.launch()
        try:
            for width in (1440,390):
                context=browser.new_context(viewport={"width":width,"height":1000})
                context.add_init_script("""localStorage.setItem('zeus_access_code','offline-test');
                  window.WebSocket=class {static OPEN=1;static CONNECTING=0;readyState=1;constructor(){window.__testSocket=this;}close(){}send(){}};""")
                page=context.new_page();sent=[];bots=[];errors=[]
                page.on("pageerror",lambda error:errors.append(str(error)))
                def route(call):
                    url=urlparse(call.request.url)
                    if url.hostname!="zeus.test":
                        call.abort();return
                    if url.path.startswith("/api/"):
                        data={"running":False,"bots":[]}
                        if url.path=="/api/grid/tokens":
                            data={"tokens":[{"key":"SP500","symbol":"xyz:SP500","dex":"xyz","price":100}]}
                        elif url.path=="/api/grid/status":
                            data={"type":"grid_stats","running":bool(bots),"bots":bots}
                        elif url.path=="/api/grid/start":
                            payload=call.request.post_data_json;sent.append(payload)
                            bots.append({**payload,"running":True,"phase":"running","duration":"00:00:01",
                                "execution_policy":"maker_only_auto","execution_transport":"websocket",
                                "position_known":True,"position_usd":0,"accounting_complete":True,"net_pnl":0,
                                "volume_usd":0,"fills":0,"fees":0,"cleanup_confirmed":False,"can_start":False})
                            data={"status":"starting"}
                        call.fulfill(json=data);return
                    name=url.path.lstrip("/").removeprefix("frontend/") or "index.html"
                    file=ROOT/"frontend"/name
                    if not file.is_file():call.fulfill(status=404,body="");return
                    call.fulfill(body=file.read_bytes(),content_type=mimetypes.guess_type(name)[0] or "text/plain")
                page.route("**/*",route)
                page.goto("https://zeus.test/")
                page.get_by_label("Mode", exact=True).select_option("mid")
                checkbox=page.get_by_role("checkbox",name="Break-even exits")
                expect(checkbox).to_be_visible();expect(checkbox).to_be_checked()
                checkbox.focus();page.keyboard.press("Space");expect(checkbox).not_to_be_checked()
                page.keyboard.press("Space");expect(checkbox).to_be_checked()
                page.locator("#grid-start-btn").click()
                page.wait_for_function("document.querySelector('#grid-start-btn').getAttribute('aria-busy')!=='true'")
                assert sent and sent[0]["mid_protect_exits"] is True,sent
                assert sent[0]["mid_exit_protect_sec"]==30
                assert sent[0]["order_size_usd"]==100
                page.evaluate("data=>window.__testSocket.onmessage({data:JSON.stringify(data)})",{"type":"grid_stats","running":True,"bots":bots})
                expect(page.locator('[data-f="title"]')).to_contain_text("EXIT GUARD 30S",timeout=10000)
                expect(page.locator('[data-f="title"]')).to_contain_text("MAKER / WS")
                checkbox.uncheck()
                expect(page.locator('[data-f="title"]')).to_contain_text("EXIT GUARD 30S")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                folder=ROOT/"artifacts"/"mid-audit";folder.mkdir(exist_ok=True)
                page.screenshot(path=str(folder/f"pnl-ui-{width}.png"),full_page=True)
                page.get_by_label("Mode", exact=True).select_option("bbo")
                expect(checkbox).not_to_be_visible();expect(page.locator("#grid-protect-exits")).to_be_disabled()
                assert not errors,errors
                context.close()
        finally:browser.close()
    print("UI OK: keyboard, MID payload, immutable session label, BBO unchanged, 1440/390 px; all requests mocked")


if __name__=="__main__":main()
