// Mini App checkout prefetch: the XPAY checkout starts while the page opens, so
// «Оплата» only waits for the (DB-only) /start and then opens the ready link.
const vm = require('node:vm');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = JSON.parse(fs.readFileSync(0, 'utf8'));
const script = html.split('<script>')[1].split('</script>')[0];
const response = data => ({ok: true, json: async () => data});

function setup(fetchImpl) {
  const nodes = {}, opened = [], calls = [];
  const markup = id => html.match(new RegExp('<[^>]+id="' + id + '"[^>]*>'))?.[0] || '';
  const node = id => nodes[id] ||= {
    hidden: /class="[^"]*\bhidden\b/.test(markup(id)),
    disabled: /\sdisabled(?:\s|>)/.test(markup(id)),
    classList: {toggle(name, hidden) { if (name === 'hidden') nodes[id].hidden = hidden; }, contains() { return nodes[id].hidden; }},
    attributes: {}, setAttribute(key, value) { this.attributes[key] = value; },
    checked: false, click() {}, open: false, listeners: {},
    addEventListener(event, fn) { this.listeners[event] = fn; },
    showModal() { this.open = true; }, close() { this.open = false; },
    textContent: '', replaceChildren() {}, appendChild() {},
  };
  const context = {
    URLSearchParams, AbortController,
    location: {search: '?tgWebAppData=test&tgWebAppStartParam=test', hash: ''},
    window: {Telegram: {WebApp: {ready() {}, expand() {}, openLink(url) { opened.push(url); }, openTelegramLink() {}}}},
    document: {hidden: false, listeners: {}, getElementById: node,
      addEventListener(event, fn) { this.listeners[event] = fn; }, querySelectorAll() { return []; }},
    setTimeout() { return 1; }, clearTimeout() {},
    fetch: (path, options) => { const body = JSON.parse(options.body); calls.push([path.split('/').pop(), body.plan]); return fetchImpl(path, body); },
  };
  vm.runInNewContext(script.replace(/const paymentUrls = .*;/, 'const paymentUrls = {};'), context);
  return {node, opened, calls};
}
const url = plan => 'https://pay.xpay.kg/' + plan;
const tick = () => new Promise(setImmediate);

(async () => {
  assert.match(html, /const prefetchCheckout = true;/);
  // 1. /prepare starts together with /session, before any click, and silently.
  let finishPrepare;
  const page = setup((path, body) => {
    if (path.endsWith('/prepare')) return new Promise(resolve => { finishPrepare = () => resolve(response({payment_url: url(body.plan), expires_at_ms: Date.now() + 300000})); });
    if (path.endsWith('/start')) return Promise.resolve(response({status: 'awaiting_receipt', plan: body.plan, payment_url: url(body.plan)}));
    return Promise.resolve(response({status: 'unpaid'}));
  });
  await tick();
  assert.deepEqual(page.calls.map(c => c[0]).sort(), ['prepare', 'session']);
  assert.equal(page.node('pay-now').disabled, false, 'button must not wait for the checkout');
  // 2. A click while the checkout is still being created joins it: one /prepare, one /start.
  const clicking = page.node('pay-now').onclick();
  await tick();
  assert.deepEqual(page.opened, []);
  finishPrepare();
  await clicking;
  assert.deepEqual(page.calls.filter(c => c[0] === 'prepare').length, 1);
  assert.deepEqual(page.calls.filter(c => c[0] === 'start').length, 1);
  assert.deepEqual(page.opened, [url('weekly_7d')]);

  // 3. A failed warm-up is silent and the click still works through /start.
  const failing = setup((path, body) => {
    if (path.endsWith('/prepare')) return Promise.resolve({ok: false, status: 503, json: async () => ({detail: 'Не удалось подготовить оплату.'})});
    if (path.endsWith('/start')) return Promise.resolve(response({status: 'awaiting_receipt', plan: body.plan, payment_url: url('fallback')}));
    return Promise.resolve(response({status: 'unpaid'}));
  });
  await tick();
  assert.equal(failing.node('status').hidden, true);
  await failing.node('pay-now').onclick();
  assert.deepEqual(failing.opened, [url('fallback')]);

  // 4. Switching tariff releases the unopened checkout, then prefetches the new one.
  const order = [];
  const switching = setup((path, body) => {
    order.push(path.split('/').pop() + ':' + body.plan);
    if (path.endsWith('/prepare')) return Promise.resolve(response({payment_url: url(body.plan), expires_at_ms: Date.now() + 300000}));
    if (path.endsWith('/discard')) return Promise.resolve(response({discarded: true}));
    if (path.endsWith('/start')) return Promise.resolve(response({status: 'awaiting_receipt', plan: body.plan, payment_url: url(body.plan)}));
    return Promise.resolve(response({status: 'unpaid'}));
  });
  await tick();
  switching.node('pay-lifetime').onclick();
  await tick(); await tick();
  await switching.node('pay-now').onclick();
  assert.deepEqual(order.filter(x => !x.startsWith('session')),
    ['prepare:weekly_7d', 'discard:weekly_7d', 'prepare:lifetime', 'start:lifetime']);
  assert.deepEqual(switching.opened, [url('lifetime')]);
  console.log('Mini App prefetch regression checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
