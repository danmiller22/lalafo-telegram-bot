const vm = require('node:vm');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = JSON.parse(fs.readFileSync(0, 'utf8'));
const script = html.split('<script>')[1].split('</script>')[0];
const response = data => ({ok: true, json: async () => data});

function setup(initial, fetchOverride, dynamic = false) {
  const nodes = {}, timers = new Map(), opened = [];
  let nextTimer = 1;
  const markup = id => html.match(new RegExp('<[^>]+id="' + id + '"[^>]*>'))?.[0] || '';
  const node = id => nodes[id] ||= {
    hidden: /class="[^"]*\bhidden\b/.test(markup(id)),
    disabled: /\sdisabled(?:\s|>)/.test(markup(id)),
    classList: {
      toggle(_, hidden) { nodes[id].hidden = hidden; },
      contains() { return nodes[id].hidden; },
    },
    checked: false, click() {}, open: false,
    showModal() { this.open = true; }, close() { this.open = false; },
    textContent: '', replaceChildren() {}, appendChild() {},
  };
  const context = {
    URLSearchParams,
    FileReader: class { readAsDataURL() { this.result = "data:application/pdf;base64,JVBERi0xLjQ="; this.onload(); } },
    location: {search: '?tgWebAppData=test&tgWebAppStartParam=test', hash: ''},
    window: {Telegram: {WebApp: {ready() {}, expand() {}, openLink(url) { opened.push(url); }, openTelegramLink(url) { opened.push(url); }}}},
    document: {getElementById: node, addEventListener() {}, querySelectorAll() { return []; }},
    setTimeout(fn) { const id = nextTimer++; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch: fetchOverride || (() => Promise.resolve(response(initial))),
  };
  vm.runInNewContext(dynamic ? script.replace(/const paymentUrls = .*;/, "const paymentUrls = {};") : script, context);
  return {node, timers, opened};
}

(async () => {
  // Paint the tariff before either Telegram initialization or a slow DB response.
  let finishSession;
  const slow = setup(null, () => new Promise(resolve => { finishSession = resolve; }));
  assert.equal(slow.node('tariff-description').hidden, false);
  assert.equal(slow.node('agreement-caption').hidden, false);
  assert.equal(slow.node('pay-lifetime').hidden, false);
  assert.equal(slow.node('pay-lifetime').disabled, true);
  assert.equal(slow.node('phone').hidden, true);
  await slow.node('pay-lifetime').onclick();
  assert.deepEqual(slow.opened, []);
  await new Promise(setImmediate);
  finishSession(response({status: 'approved', phone: '+996555000000', apartment: {}}));
  await new Promise(setImmediate);
  assert.equal(slow.node('tariff-description').hidden, true);
  assert.equal(slow.node('pay-lifetime').hidden, true);
  assert.equal(slow.node('phone').hidden, false);

  const failed = setup(null, () => Promise.reject(new Error('Service unavailable')));
  await new Promise(setImmediate);
  assert.equal(failed.node('pay-lifetime').disabled, true);
  assert.equal(failed.node('phone').hidden, true);
  assert.equal(failed.node('status').textContent, 'Service unavailable');
  await failed.node('pay-lifetime').onclick();
  assert.deepEqual(failed.opened, []);

  // A restored weekly checkout must reopen its URL without a new request.
  const restored = setup({status: 'awaiting_receipt', plan: 'week'});
  await new Promise(setImmediate);
  assert.equal(restored.node('reopen-payment').hidden, false);
  assert.equal(restored.node('checkout').hidden, false);
  assert.equal(restored.node('pay-lifetime').hidden, true);
  restored.node('reopen-payment').onclick();
  restored.node('reopen-payment').onclick();
  assert.deepEqual(restored.opened, ['https://example.com/week', 'https://example.com/week']);

  let sessionCalls = 0, resolvePoll;
  const race = setup(null, path => {
    if (path.endsWith('/session')) {
      if (++sessionCalls === 1) return Promise.resolve(response({status: 'unpaid'}));
      return new Promise(resolve => { resolvePoll = resolve; });
    }
    if (path.endsWith('/consent')) return Promise.resolve(response({terms_accepted: true}));
    if (path.endsWith('/start')) return Promise.resolve(response({status: 'awaiting_receipt', plan: 'week'}));
    return Promise.resolve(response({status: 'pending', plan: 'week'}));
  });
  await new Promise(setImmediate);
  assert.equal(race.node('pay-lifetime').hidden, false);
  assert.equal(race.node('tariff-description').hidden, false);
  assert.equal(race.node('access').hidden, true);
  assert.equal(race.node('reopen-payment').hidden, true);
  assert.equal(race.node('privacy').hidden, false);
  assert.equal(race.node('agreement-caption').hidden, false);
  race.node('terms-link').onclick({preventDefault() {}});
  assert.equal(race.node('agreement').open, true);
  race.node('close-agreement').onclick();
  assert.equal(race.node('agreement').open, false);
  assert.equal(race.node('checkout').hidden, true);
  await race.node('pay-lifetime').onclick();
  assert.deepEqual(race.opened, ['https://example.com/week']);
  const [id, callback] = [...race.timers.entries()][0];
  race.timers.delete(id);
  const inFlight = callback();
  assert.equal(race.node('phone').hidden, true);
  await race.node('access').onclick();
  assert.equal(race.node('phone').hidden, true);
  assert.equal(race.node('status').textContent, 'Оплата отправлена на проверку. Ожидайте подтверждения.');
  resolvePoll(response({status: 'approved', phone: '+996555000000', apartment: {}}));
  await inFlight;
  assert.equal(race.node('phone').hidden, false);
  assert.equal(race.node('access').hidden, true);
  assert.equal(race.node('reopen-payment').hidden, true);
  assert.equal(race.timers.size, 0);
  assert.equal(race.node('status').hidden, true);
  const pending = setup({status: 'pending', plan: 'week'});
  await new Promise(setImmediate);
  assert.equal(pending.node('phone').hidden, true);
  assert.equal(pending.node('access').hidden, false);
  assert.equal(pending.node('reopen-payment').hidden, true);
  assert.equal(pending.node('status').textContent, 'Оплата отправлена на проверку. Ожидайте подтверждения.');
  assert.equal(pending.timers.size, 1);
  let finishStart;
  const warm = setup(null, path => {
    if (path.endsWith('/prepare')) return Promise.resolve(response({payment_url: 'https://example.com/prepared', expires_at_ms: Date.now() + 300000}));
    if (path.endsWith('/start')) return new Promise(resolve => { finishStart = resolve; });
    return Promise.resolve(response({status: 'unpaid'}));
  }, true);
  await new Promise(setImmediate);
  assert.equal(warm.node('pay-lifetime').disabled, false);
  const opening = warm.node('pay-lifetime').onclick();
  // Navigation happens synchronously, while the activation request is still unresolved.
  assert.deepEqual(warm.opened, ['https://example.com/prepared']);
  finishStart(response({status: 'awaiting_receipt', plan: 'week'}));
  await opening;
  assert.equal(warm.node('access').hidden, false);
  console.log('Mini App UI regression checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
