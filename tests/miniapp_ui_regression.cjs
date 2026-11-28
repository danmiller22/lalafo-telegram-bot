const vm = require('node:vm');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = JSON.parse(fs.readFileSync(0, 'utf8'));
const script = html.split('<script>')[1].split('</script>')[0];
const response = data => ({ok: true, json: async () => data});

function setup(initial, fetchOverride, dynamic = false, overrides = {}) {
  const nodes = {}, timers = new Map(), opened = [];
  let nextTimer = 1;
  const markup = id => html.match(new RegExp('<[^>]+id="' + id + '"[^>]*>'))?.[0] || '';
  const node = id => nodes[id] ||= {
    hidden: /class="[^"]*\bhidden\b/.test(markup(id)),
    disabled: /\sdisabled(?:\s|>)/.test(markup(id)),
    classList: {
      toggle(name, hidden) { if (name === "hidden") nodes[id].hidden = hidden; },
      contains() { return nodes[id].hidden; },
    },
    attributes: {}, setAttribute(key, value) { this.attributes[key] = value; },
    checked: false, click() {}, open: false,
    listeners: {}, addEventListener(event, fn) { this.listeners[event] = fn; },
    showModal() { this.open = true; }, close() { this.open = false; },
    textContent: '', replaceChildren() {}, appendChild() {},
  };
  const context = {
    URLSearchParams,
    AbortController,
    FileReader: class { readAsDataURL() { this.result = "data:application/pdf;base64,JVBERi0xLjQ="; this.onload(); } },
    location: {search: '?tgWebAppData=test&tgWebAppStartParam=test', hash: ''},
    window: {Telegram: {WebApp: {ready() {}, expand() {}, openLink(url) { opened.push(url); }, openTelegramLink(url) { opened.push(url); }}}},
    document: {hidden: false, listeners: {}, getElementById: node,
      addEventListener(event, fn) { this.listeners[event] = fn; }, querySelectorAll() { return []; }},
    setTimeout(fn) { const id = nextTimer++; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch: fetchOverride || (() => Promise.resolve(response(initial))),
    ...overrides,
  };
  vm.runInNewContext(dynamic ? script.replace(/const paymentUrls = .*;/, "const paymentUrls = {};") : script, context);
  return {node, timers, opened, context};
}

(async () => {
  const weekly = setup(null, (path, options) => {
    const body = JSON.parse(options.body);
    if (path.endsWith('/prepare')) return Promise.resolve(response({payment_url:'https://qr.finik.kg/week-499',expires_at_ms:Date.now()+300000}));
    if (path.endsWith('/start')) {
      assert.equal(body.plan, 'weekly_7d');
      return Promise.resolve(response({status:'awaiting_receipt',plan:'weekly_7d',price:499,payment_url:'https://qr.finik.kg/week-499'}));
    }
    return Promise.resolve(response({status:'unpaid'}));
  });
  await new Promise(setImmediate);
  await weekly.node('pay-weekly').onclick();
  assert.equal(weekly.opened.length, 0, 'Selecting the weekly tariff must not open checkout');
  await weekly.node('pay-now').onclick();
  assert.deepEqual(weekly.opened, ['https://qr.finik.kg/week-499']);
  assert.equal(weekly.node('intro').textContent, '7 дней · 499 сом');
  assert.equal(weekly.node('pay-lifetime').hidden, true);
  const unavailable = setup(null, () => Promise.resolve({ok: false, status: 404,
    json: async () => ({detail: 'Квартира больше недоступна.'})}));
  await new Promise(setImmediate);
  assert.equal(unavailable.node('pay-lifetime').hidden, true);
  assert.equal(unavailable.node('other-apartments').hidden, false);
  await unavailable.node('pay-now').onclick();
  assert.deepEqual(unavailable.opened, []);
  // An apartment may become unavailable between page load and checkout start.
  for (const status of ['unavailable', 'pending', 'approved']) {
    const changed = setup(null, path => Promise.resolve(response(path.endsWith('/start') ?
      {status, plan: 'weekly_7d', apartment: {}} : {status: 'unpaid'})));
    await new Promise(setImmediate);
    await changed.node('pay-now').onclick();
    assert.deepEqual(changed.opened, [], status + ' must not redirect to Finik');
  }
  let lateCalls = 0;
  const late = setup({status: 'unpaid'}, () => {
    lateCalls++;
    return Promise.resolve(response({status: 'unpaid'}));
  }, false, {location: {search: '', hash: ''}, window: {}});
  for (let i = 0; i < 20; i++) {
    const [id, callback] = [...late.timers.entries()][0];
    late.timers.delete(id);
    callback();
    await new Promise(setImmediate);
  }
  assert.equal(lateCalls, 0);
  assert.equal(late.node('pay-lifetime').disabled, false);
  late.context.window.Telegram = {WebApp: {
    initData: 'start_param=late-token', initDataUnsafe: {start_param: 'late-token'},
    ready() {}, expand() {}, openLink(url) { late.opened.push(url); },
  }};
  assert.equal(typeof late.node('telegram-sdk').listeners.load, 'function');
  late.node('telegram-sdk').listeners.load();
  await new Promise(setImmediate);
  assert.equal(lateCalls, 1);
  assert.equal(late.node('pay-lifetime').disabled, false);
  assert.equal(late.node('status').hidden, true);

  let embeddedBody;
  const embedded = setup({status: 'unpaid'}, (_, options) => {
    embeddedBody = JSON.parse(options.body);
    return Promise.resolve(response({status: 'unpaid'}));
  }, false, {location: {search: '?tgWebAppData=' + encodeURIComponent('start_param=embedded-token&auth_date=1700000000'), hash: ''}});
  await new Promise(setImmediate);
  assert.equal(embeddedBody.start_param, 'embedded-token');
  assert.equal(embedded.node('pay-lifetime').disabled, false);

  // Finik may have a persisted checkout even if the start response was lost.
  // Recover its status and allow the claim without awaiting the old rejection.
  let checkoutPersisted = false, recoveredClaims = 0;
  const recovery = setup(null, (path) => {
    if (path.endsWith('/start')) {
      checkoutPersisted = true;
      return Promise.reject(new Error('Start response lost'));
    }
    if (path.endsWith('/access')) {
      recoveredClaims++;
      return Promise.resolve(response({status: 'pending', plan: 'weekly_7d'}));
    }
    return Promise.resolve(response(checkoutPersisted ?
      {status: 'awaiting_receipt', plan: 'weekly_7d'} : {status: 'unpaid'}));
  });
  await new Promise(setImmediate);
  await recovery.node('pay-now').onclick();
  assert.equal(recovery.node('pay-lifetime').disabled, false);
  assert.equal(recovery.timers.size, 1, 'An ambiguous start must resume status polling');
  const [recoveryId, recoveryPoll] = [...recovery.timers.entries()][0];
  recovery.timers.delete(recoveryId);
  await recoveryPoll();
  assert.equal(recovery.node('access').hidden, false);
  await recovery.node('access').onclick();
  assert.equal(recoveredClaims, 1, 'A stale rejected start promise must not block the claim');

  // A stalled mobile request must release the button and offer a retry.
  let aborted = false;
  const hanging = setup(null, (path, options) => {
    if (!path.endsWith('/start')) return Promise.resolve(response({status: 'unpaid'}));
    return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => {
      aborted = true;
      reject(Object.assign(new Error('aborted'), {name: 'AbortError'}));
    }));
  });
  await new Promise(setImmediate);
  const hungStart = hanging.node('pay-now').onclick();
  const [timeoutId, requestTimeout] = [...hanging.timers.entries()][0];
  hanging.timers.delete(timeoutId);
  requestTimeout();
  await hungStart;
  assert.equal(aborted, true);
  assert.equal(hanging.node('pay-lifetime').disabled, false);
  assert.match(hanging.node('status').textContent, /Попробуйте ещё раз/);

  // Paint the tariff before either Telegram initialization or a slow DB response.
  let finishSession;
  const slow = setup(null, path => path.endsWith('/session') ? new Promise(resolve => { finishSession = resolve; }) : Promise.resolve(response({status: 'awaiting_receipt', plan: 'weekly_7d'})));
  assert.equal(slow.node('tariff-description').hidden, false);
  assert.equal(slow.node('agreement-caption').hidden, false);
  assert.equal(slow.node('pay-lifetime').hidden, false);
  assert.equal(slow.node('pay-lifetime').disabled, false);
  assert.equal(slow.node('phone').hidden, true);
  await slow.node('pay-now').onclick();
  assert.deepEqual(slow.opened, ['https://example.com/week']);
  await new Promise(setImmediate);
  finishSession(response({status: 'approved', phone: '+996555000000', apartment: {}}));
  await new Promise(setImmediate);
  assert.equal(slow.node('tariff-description').hidden, true);
  assert.equal(slow.node('pay-lifetime').hidden, true);
  assert.equal(slow.node('phone').hidden, false);

  const failed = setup(null, () => Promise.reject(new Error('Service unavailable')));
  await new Promise(setImmediate);
  assert.equal(failed.node('pay-lifetime').disabled, false);
  assert.equal(failed.node('phone').hidden, true);
  assert.equal(failed.node('status').textContent, 'Service unavailable');
  await failed.node('pay-now').onclick();
  assert.deepEqual(failed.opened, []);

  // Reopening must validate the checkout even if a stale URL remains cached
  // after a suspended WebView. Never send a customer back to an expired QR.
  let reopenCalls = 0, finishReopen;
  const restored = setup(null, path => {
    if (path.endsWith('/start')) {
      reopenCalls++;
      return new Promise(resolve => { finishReopen = resolve; });
    }
    return Promise.resolve(response({status: 'awaiting_receipt', plan: 'weekly_7d'}));
  });
  await new Promise(setImmediate);
  assert.equal(restored.node('reopen-payment').hidden, false);
  const reopening = restored.node('reopen-payment').onclick();
  await restored.node('reopen-payment').onclick();
  assert.equal(reopenCalls, 1, 'double-click must not create concurrent checkouts');
  assert.deepEqual(restored.opened, []);
  finishReopen(response({status: 'awaiting_receipt', plan: 'weekly_7d', payment_url: 'https://example.com/renewed'}));
  await reopening;
  assert.deepEqual(restored.opened, ['https://example.com/renewed']);
  for (const status of ['approved', 'pending', 'unavailable']) {
    const completed = setup(null, path => Promise.resolve(response(path.endsWith('/start') ?
      {status, plan: 'weekly_7d', apartment: {}} : {status: 'awaiting_receipt', plan: 'weekly_7d'})));
    await new Promise(setImmediate);
    await completed.node('reopen-payment').onclick();
    assert.deepEqual(completed.opened, [], status + ' must not reopen cached checkout');
  }

  let sessionCalls = 0, resolvePoll;
  const race = setup(null, path => {
    if (path.endsWith('/session')) {
      if (++sessionCalls === 1) return Promise.resolve(response({status: 'unpaid'}));
      return new Promise(resolve => { resolvePoll = resolve; });
    }
    if (path.endsWith('/consent')) return Promise.resolve(response({terms_accepted: true}));
    if (path.endsWith('/start')) return Promise.resolve(response({status: 'awaiting_receipt', plan: 'weekly_7d'}));
    return Promise.resolve(response({status: 'pending', plan: 'weekly_7d'}));
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
  await race.node('pay-now').onclick();
  assert.deepEqual(race.opened, ['https://example.com/week']);
  const [id, callback] = [...race.timers.entries()][0];
  race.timers.delete(id);
  const inFlight = callback();
  assert.equal(race.node('phone').hidden, true);
  await race.node('access').onclick();
  assert.equal(race.node('phone').hidden, true);
  assert.equal(race.node('status').textContent, 'Заявка отправлена на проверку. Ожидайте подтверждения.');
  resolvePoll(response({status: 'approved', phone: '+996555000000', apartment: {}}));
  await inFlight;
  assert.equal(race.node('phone').hidden, false);
  assert.equal(race.node('access').hidden, true);
  assert.equal(race.node('reopen-payment').hidden, true);
  assert.equal(race.timers.size, 0);
  assert.equal(race.node('status').hidden, true);
  const pending = setup({status: 'pending', plan: 'weekly_7d'});
  await new Promise(setImmediate);
  assert.equal(pending.node('phone').hidden, true);
  assert.equal(pending.node('access').hidden, true);
  assert.equal(pending.node('reopen-payment').hidden, true);
  assert.equal(pending.node('status').textContent, 'Заявка отправлена на проверку. Ожидайте подтверждения.');
  assert.equal(pending.timers.size, 1);
  let finishStart;
  const warm = setup(null, path => {
    if (path.endsWith('/prepare')) return Promise.resolve(response({payment_url: 'https://example.com/prepared', expires_at_ms: Date.now() + 300000}));
    if (path.endsWith('/start')) return new Promise(resolve => { finishStart = resolve; });
    return Promise.resolve(response({status: 'unpaid'}));
  }, true);
  await new Promise(setImmediate);
  assert.equal(warm.node('pay-lifetime').disabled, false);
  const opening = warm.node('pay-now').onclick();
  // The checkout request is persisted before Telegram opens Finik.
  assert.deepEqual(warm.opened, []);
  assert.equal(warm.node('pay-now').attributes['aria-busy'], 'true');
  assert.equal(warm.node('pay-now').textContent, '');
  finishStart(response({status: 'awaiting_receipt', plan: 'weekly_7d', payment_url:'https://example.com/prepared'}));
  await opening;
  assert.deepEqual(warm.opened, ['https://example.com/prepared']);
  assert.equal(warm.node('pay-now').attributes['aria-busy'], 'false');
  assert.equal(warm.node('access').hidden, false);
  // Checkout is created for the chosen tariff on click, without a competing
  // background preparation that could replace another selected tariff.
  let finishPreparation, prepareCalls = 0, startCalls = 0;
  const slowWarmup = setup(null, path => {
    if (path.endsWith('/prepare')) {
      prepareCalls++;
      return new Promise(resolve => { finishPreparation = resolve; });
    }
    if (path.endsWith('/start')) {
      startCalls++;
      return Promise.resolve(response({status: 'awaiting_receipt', plan: 'weekly_7d', payment_url: 'https://example.com/fast'}));
    }
    return Promise.resolve(response({status: 'unpaid'}));
  }, true);
  await new Promise(setImmediate);
  assert.equal(slowWarmup.node('pay-now').disabled, false);
  const slowClick = slowWarmup.node('pay-now').onclick();
  assert.equal(startCalls, 1);
  await slowClick;
  assert.equal(prepareCalls, 0);
  assert.equal(startCalls, 1);
  assert.deepEqual(slowWarmup.opened, ['https://example.com/fast']);
  // Back returns to tariffs without cancelling or losing the payment request.
  restored.node('back').onclick();
  assert.equal(restored.node('tariff-description').hidden, false);
  assert.equal(restored.node('checkout').hidden, true);
  assert.equal(restored.node('resume-payment').hidden, false);
  restored.node('resume-payment').onclick();
  assert.equal(restored.node('checkout').hidden, false);
  assert.equal(restored.node('access').hidden, false);
  restored.node('terms-link').onclick({preventDefault() {}});
  restored.node('back').onclick();
  assert.equal(restored.node('agreement').open, false);
  assert.equal(restored.node('checkout').hidden, false);

  let finishBackStart;
  const backDuringStart = setup(null, path => {
    if (path.endsWith('/start')) return new Promise(resolve => { finishBackStart = resolve; });
    return Promise.resolve(response({status:'unpaid'}));
  });
  await new Promise(setImmediate);
  const backOpening = backDuringStart.node('pay-now').onclick();
  backDuringStart.node('back').onclick();
  finishBackStart(response({status:'awaiting_receipt',plan:'weekly_7d'}));
  await backOpening;
  assert.deepEqual(backDuringStart.opened, [], 'Back must prevent a late Finik redirect');
  assert.equal(backDuringStart.node('tariff-description').hidden, false);
  backDuringStart.node('resume-payment').onclick();
  assert.equal(backDuringStart.node('access').hidden, false);

  let unpaidRequests = 0;
  const idle = setup(null, () => { unpaidRequests++; return Promise.resolve(response({status:'unpaid'})); });
  await new Promise(setImmediate);
  for (let i = 0; i < 3; i++) {
    idle.context.document.hidden = true;
    idle.context.document.listeners.visibilitychange();
    idle.context.document.hidden = false;
    idle.context.document.listeners.visibilitychange();
  }
  assert.equal(unpaidRequests, 1, 'Unpaid browsing must not start payment polling');
  assert.equal(idle.timers.size, 0);
  pending.context.document.hidden = true;
  pending.context.document.listeners.visibilitychange();
  assert.equal(pending.timers.size, 0, 'Hidden Mini Apps must pause payment polling');
  pending.context.document.hidden = false;
  pending.context.document.listeners.visibilitychange();
  assert.equal(pending.timers.size, 1);

  let nativeHandler, nativeBindings = 0, closed = 0, readyCalls = 0;
  const native = setup(null, null, false, {window:{Telegram:{WebApp:{
    initData:'test', initDataUnsafe:{start_param:'test'},
    ready(){readyCalls++;}, expand(){}, close(){closed++;},
    BackButton:{onClick(fn){nativeHandler=fn;nativeBindings++;},show(){}},
  }}}});
  await new Promise(setImmediate);
  native.node('telegram-sdk').listeners.load();
  assert.equal(nativeBindings, 1, 'Bind the Telegram back handler only once');
  assert.equal(readyCalls, 1);
  assert.equal(native.node('back').hidden, true);
  nativeHandler();
  assert.equal(closed, 1);
  const olderTelegram = setup(null, null, false, {window:{Telegram:{WebApp:{
    initData:'test',initDataUnsafe:{start_param:'test'},ready(){},expand(){},
    isVersionAtLeast(){return false;},
    BackButton:{onClick(){throw Error('unsupported');},show(){throw Error('unsupported');}},
  }}}});
  await new Promise(setImmediate);
  assert.equal(olderTelegram.node('back').hidden, false);
  assert.equal(olderTelegram.node('pay-now').hidden, false);
  const legacyLifetime = setup({status:'approved',plan:'lifetime',phone:'+996555000000',apartment:{}});
  await new Promise(setImmediate);
  assert.equal(legacyLifetime.node('phone').hidden, false, 'Existing lifetime access remains usable');
  assert.equal(legacyLifetime.node('pay-now').hidden, true);
  // An already-started legacy payment can still settle and open setup.
  let personalPaid = false;
  const personal = setup(null, (path, options) => {
    const body = JSON.parse(options.body);
    assert.ok(path.endsWith('/session'));
    if (personalPaid) return Promise.resolve(response({status:'approved', plan:'personal',
      personal_setup_url:'https://t.me/testbot?start=personal', phone:'+996555000000', apartment:{}}));
    return Promise.resolve(response({status:'awaiting_receipt', plan:'personal', automatic_payment:true}));
  }, true);
  await new Promise(setImmediate);
  assert.deepEqual(personal.opened, []);
  assert.equal(personal.node('intro').textContent, 'Подтверждение ранее начатой оплаты');
  personalPaid = true;
  const [personalTimer, personalPoll] = [...personal.timers.entries()][0];
  personal.timers.delete(personalTimer);
  await personalPoll();
  assert.equal(personal.node('personal-setup').hidden, false);
  assert.equal(personal.opened.at(-1), 'https://t.me/testbot?start=personal');
  const paywall = setup(null, (path, options) => {
    assert.equal(JSON.parse(options.body).plan, 'weekly_7d');
    return Promise.resolve(response({status:'unpaid',plan:'weekly_7d',automatic_payment:true}));
  }, true, {location:{search:'?tgWebAppData=test&tgWebAppStartParam=personal_signed-token',hash:''}});
  await new Promise(setImmediate);
  assert.equal(paywall.node('pay-weekly').attributes['aria-pressed'], 'true');
  console.log('Mini App UI regression checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
