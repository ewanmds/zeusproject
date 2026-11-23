// Playwright CLI run-code fixture. Serve frontend/workspace/dist on localhost:8793.
// Every API/WS response is fake; non-local traffic and unexpected mutations abort.
async (page) => {
  const origin = 'http://127.0.0.1:8793', owner = '0x'+'1'.repeat(40);
  let revision = 0, held = null, socket = null, stops = 0;
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const row = id => ({id, kind:'outcome', strategy:'outcome', name:'Offline '+id, market:'#101',
    phase:'running', running:true, cleanup_confirmed:false, started:Date.now()/1000-60,
    duration_sec:60, volume_usd:100, net_pnl:0, fees:0, fills:2, cost_per_1m:0,
    position_usd:70, stale:false, accounting_complete:true, state:'Bid working', reason:'', rotations:0,
    config:{order_size_usd:70, max_position_usd:120, exec_mode:'maker'}, quotes:[]});
  const rows = [row('success'), row('error'), row('timeout')];
  const snapshot = () => ({type:'workspace', owner, source_id:'offline-instant-stop', revision:++revision,
    published_at:Date.now()/1000, sessions:rows, metrics:{active:3, volume_usd:300,
      net_pnl:0, fees:0, fills:6, accounting_complete:true}});
  const send = () => socket?.send(JSON.stringify(snapshot()));
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) return route.abort();
    if (!url.pathname.startsWith('/api/')) return route.continue();
    if (request.method() === 'POST') {
      if (!url.pathname.endsWith('/stop')) throw Error('Unexpected mutation: '+url.pathname);
      if (request.postDataJSON().mode !== 'cancel_and_market') throw Error('Missing explicit taker stop intent');
      stops++;
      if (url.pathname.includes('/success/')) { held = route; return; }
      if (url.pathname.includes('/error/')) return route.fulfill({status:503, json:{detail:'Offline cancellation failure'}});
      return route.fulfill({json:snapshot()}); // accepted intent, never a flat confirmation
    }
    let data = {};
    if (url.pathname.endsWith('/config')) data = {version:'workspace-1', features:{}};
    else if (url.pathname.endsWith('/me')) data = {wallet:owner, csrf:'offline', agent:{status:'ready'}};
    else if (url.pathname.endsWith('/account')) data = {usdc_available:253.84};
    else if (url.pathname.endsWith('/workspace')) data = snapshot();
    else if (url.pathname.endsWith('/markets')) data = {outcomes:[]};
    else if (url.pathname.includes('/scanner')) data = {ready:false, candidates:[], observed_markets:[]};
    await route.fulfill({json:data});
  });
  await page.routeWebSocket('**/api/v2/ws', ws => { socket = ws; send(); });
  await page.clock.install();
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(origin+'/#sessions');
  const card = name => page.locator('.session-card').filter({hasText:'Offline '+name});
  await card('success').waitFor();
  await card('success').getByRole('button', {name:'Arrêter', exact:true}).click();
  await page.waitForFunction(() => document.querySelectorAll('.session-card').length === 2);
  if (await page.getByRole('alertdialog').count()) throw Error('Stop still needs confirmation');
  if (await card('success').count()) throw Error('Card was not hidden before HTTP response');
  if (!held || stops !== 1) throw Error('Stop did not send exactly one request');
  Object.assign(rows[0], {running:false, phase:'stopped', cleanup_confirmed:true, position_usd:0});
  await held.fulfill({json:snapshot()});
  send();
  await page.getByRole('button', {name:'Terminées', exact:true}).click();
  await card('success').waitFor(); // audit history is preserved
  await page.getByRole('button', {name:'En cours', exact:true}).click();
  await card('error').getByRole('button', {name:'Arrêter', exact:true}).click();
  await card('error').waitFor();
  await page.getByText('Offline cancellation failure', {exact:true}).waitFor();
  await card('timeout').getByRole('button', {name:'Arrêter', exact:true}).click();
  await page.waitForFunction(() => document.querySelectorAll('.session-card').length === 1);
  await page.clock.fastForward(31000);
  await card('timeout').waitFor();
  await page.getByText(/Arrêt non confirmé après 30 s/).waitFor();
  if (rows[2].cleanup_confirmed) throw Error('Unconfirmed stop was marked closed');
  await page.setViewportSize({width:390,height:900});
  if (!await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)) throw Error('Mobile overflow');
  await page.screenshot({path:'output/playwright/instant-stop-mobile.png',fullPage:true});
  if (errors.length) throw Error(errors.join('\n'));
  if (stops !== 3) throw Error('Unexpected duplicate Stop requests');
  return {oneClick:true, hiddenBeforeResponse:true, scopedTakerIntent:true, failuresRestored:true,
    timeoutVisible:true, historyPreserved:true, noTrading:true, requests:stops};
}
