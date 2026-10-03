const vm = require('node:vm');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = JSON.parse(fs.readFileSync(0, 'utf8'));
const script = html.split('<script>')[1].split('</script>')[0];
const response = data => ({ok: true, json: async () => data});

function setup(initial, fetchOverride) {
  const nodes = {}, timers = new Map(), opened = [];
  let nextTimer = 1;
  const node = id => nodes[id] ||= {
    hidden: true,
    classList: {
      toggle(_, hidden) { nodes[id].hidden = hidden; },
      contains() { return nodes[id].hidden; },
    },
    textContent: '', replaceChildren() {}, appendChild() {},
  };
  const context = {
    URLSearchParams,
    location: {search: '?tgWebAppData=test&tgWebAppStartParam=test', hash: ''},
    window: {Telegram: {WebApp: {ready() {}, expand() {}, openLink(url) { opened.push(url); }}}},
    document: {getElementById: node, addEventListener() {}},
    setTimeout(fn) { const id = nextTimer++; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch: fetchOverride || (() => Promise.resolve(response(initial))),
  };
  vm.runInNewContext(script, context);
  return {node, timers, opened};
}

(async () => {
  // A restored monthly checkout must reopen the monthly URL without a new request.
  const restored = setup({status: 'awaiting_receipt', plan: 'month'});
  await new Promise(setImmediate);
  assert.equal(restored.node('reopen-payment').hidden, false);
  restored.node('reopen-payment').onclick();
  restored.node('reopen-payment').onclick();
  assert.deepEqual(restored.opened, ['https://example.com/month', 'https://example.com/month']);

  let sessionCalls = 0, resolvePoll;
  const race = setup(null, path => {
    if (path.endsWith('/session')) {
      if (++sessionCalls === 1) return Promise.resolve(response({status: 'unpaid'}));
      return new Promise(resolve => { resolvePoll = resolve; });
    }
    if (path.endsWith('/start')) return Promise.resolve(response({status: 'awaiting_receipt', plan: 'week'}));
    return Promise.resolve(response({status: 'approved', phone: '+996555000000', apartment: {}}));
  });
  await new Promise(setImmediate);
  await race.node('pay-week').onclick();
  const [id, callback] = [...race.timers.entries()][0];
  race.timers.delete(id);
  const inFlight = callback();
  await race.node('access').onclick();
  assert.equal(race.node('phone').hidden, false);
  resolvePoll(response({status: 'awaiting_receipt'}));
  await inFlight;
  assert.equal(race.node('phone').hidden, false);
  assert.equal(race.node('access').hidden, true);
  assert.equal(race.node('reopen-payment').hidden, true);
  assert.equal(race.timers.size, 0);
  assert.equal(race.node('status').hidden, true);
  console.log('Mini App UI regression checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
