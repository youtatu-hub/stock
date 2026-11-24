const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {resolveInstrument, seedInstrumentWatchlist, assetSummaryHtml, fundTermsHtml, hasCurrentIopv, modelEstimateHtml, premiumHtml} = require('../dashboard/asset-view.js');
const esc = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');

const catalog = {
  profiles: {stock: {model: 'stock'}, listed_fund: {model: 'reference'}}, listed_fund_prefixes: ['5', '15'],
  instruments: {
    '511360': {code: '511360', model: 'defensive', seed_version: 1, name: '短融'},
    '050025': {code: '050025', model: 'nav', seed_version: 2, name: '联接A'},
    '006075': {code: '006075', model: 'nav', seed_version: 3, name: '联接C'},
    '518880': {code: '518880', model: 'technical', seed_version: 3, name: '黄金'},
  },
};

test('资产模型独立于目录；未知场内基金不成为股票', () => {
  assert.equal(resolveInstrument(catalog, 'sh511360').model, 'defensive');
  assert.equal(resolveInstrument(catalog, '159999').model, 'reference');
  assert.equal(resolveInstrument(catalog, '600000').model, 'stock');
});

test('v2迁移仅补新品种，保留删掉的旧关注和股票评分，不重复添加', () => {
  const list = [{code: '600000', action: '买入', score: 70}, {code: '518880', action: '买入', score: 80}];
  const next = seedInstrumentWatchlist(catalog, list, 2, 1);
  assert.deepEqual(next.map(item => item.code), ['600000', '518880', '006075']);
  assert.equal(next[0].score, 70);
  assert.equal(next[1].score, undefined);
  assert.equal(next[1].action, undefined);
  assert.equal(seedInstrumentWatchlist(catalog, [], 3).length, 0);
  assert.equal(seedInstrumentWatchlist(catalog, [], 0).length, 4);
  assert.equal(list[1].score, 80);
});

test('ETF摘要显示真实零回撤，不伪造0分或置信度', () => {
  const html = assetSummaryHtml({action: '资产观察', plain_summary: '测试', description: '价格参考',
    trend: {direction: '上升'}, asset_metrics: {return_pct: 0, max_drawdown_pct: 0,
      start_date: '2026-01-01', end_date: '2026-01-30', sample_count: 30, basis: '收盘价'}}, {label: '短债'}, esc);
  assert.match(html, /0\.00%/);
  assert.doesNotMatch(html, /置信度|0分|买入价|止损价/);
});

test('费用来源失败明确显示，外部文本转义，不把未知限额显示为无限', () => {
  const html = fundTermsHtml({symbol: '050025', items: [{instrument: {code: '050025', share_class: 'A'}, data: {
    trading: {subscription: '暂停申购', daily_limit: null}, fees: {subscription: [], redemption: [], sales_service: '0.00%'},
    source: '<img>', source_url: 'https://example.test', fetched_at: '2026-01-01T00:00:00Z'},
  }, {instrument: {code: '006075', share_class: 'C'}, error: '<script>失败</script>'}]}, esc);
  assert.match(html, /暂停申购/);
  assert.match(html, /日累计申购限额<\/dt><dd>未披露/);
  assert.match(html, /获取失败/);
  assert.doesNotMatch(html, /<script>|<img>|无限额/);
});

test('IOPV提醒会随时间过期，已披露净值偏离不触发提醒', () => {
  const now = Date.parse('2026-01-05T02:00:30Z');
  const data = {iopv: {status: 'ok', price: 1.1, iopv: 1, premium_pct: 10, snapshot_fresh: true, alert_eligible: true,
    iopv_fresh: true, iopv_time: '2026-01-05T02:00:00Z',
    quote_time: '2026-01-05T02:00:00Z', fresh_until: '2026-01-05T02:02:00Z', source: '测试', note: '时间可核验'},
    disclosed_nav: {value: 0.8, date: '2026-01-02', premium_pct: 37.5, source: '测试', source_url: 'https://example.test', basis: '非盘中估值'}};
  assert.match(premiumHtml(data, 3, esc, now), /参考溢价幅度达到/);
  assert.doesNotMatch(premiumHtml(data, 20, esc, now), /参考溢价幅度达到/);
  assert.doesNotMatch(premiumHtml(data, 3, esc, now + 200000), /参考溢价幅度达到/);
  assert.doesNotMatch(premiumHtml({...data, iopv: null}, 3, esc, now), /参考溢价幅度达到/);
  const unknownTime = premiumHtml({...data, iopv: {...data.iopv, status: 'unavailable', iopv: null, premium_pct: null,
    iopv_time: null, iopv_fresh: false, note: '来源没有独立估值时间',
    reported_reference: {value: 1, premium_pct: 10.04, source_price: 1.1, price_time: '2026-01-05T02:00:00Z',
      quote_fresh_until: '2026-01-05T02:02:00Z', valuation_time: null,
      fetched_at: '2026-01-05T02:00:30Z', source: '腾讯', source_url: 'https://example.test'}}}, 3, esc, now);
  assert.doesNotMatch(unknownTime, /参考溢价幅度达到/);
  assert.match(unknownTime, /腾讯 IOPV 参考.*估值时效未核验/);
  assert.doesNotMatch(unknownTime, /价格相对IOPV折溢价<\/dt>|实时IOPV<\/dt>/);
  assert.match(unknownTime, /<section class="premium-reference"/);
  assert.match(unknownTime, /最近收到的IOPV参考值<\/dt><dd>1\.0000/);
  assert.match(unknownTime, /来源参考折溢价<\/dt><dd>\+10\.04%/);
  assert.match(unknownTime, /来源快照市价<\/dt><dd>1\.1000/);
  assert.match(unknownTime, /行情快照更新时间<\/dt>/);
  assert.match(unknownTime, /IOPV计算时间<\/dt><dd>来源未提供/);
  assert.doesNotMatch(unknownTime, /<details|class="premium-alert"/);
});

test('来源折溢价缺失不自行补算；失败和过期参考保留原时间并标明旧报价', () => {
  const now = Date.parse('2026-01-05T02:00:30Z');
  const snapshot = {status: 'unavailable', snapshot_fresh: true, iopv: null, premium_pct: null,
    reported_reference: {value: 1, premium_pct: null, source_price: 1.1, price_time: '2026-01-05T02:00:00Z',
      quote_fresh_until: '2026-01-05T02:02:00Z', valuation_time: null,
      fetched_at: '2026-01-05T02:00:01Z', source: '<测试>', source_url: 'https://example.test'}};
  const html = premiumHtml({iopv: snapshot}, 3, esc, now);
  assert.match(html, /来源参考折溢价<\/dt><dd>来源未提供/);
  assert.doesNotMatch(html, /\+10\.00%|<测试>/);
  const expired = premiumHtml({iopv: snapshot}, 3, esc, now + 200000);
  assert.match(expired, /来源快照市价（旧报价）/);
  const stale = premiumHtml({iopv: {...snapshot, stale: true, refresh_error: '<offline>'}}, 3, esc, now);
  assert.match(stale, /上次成功获取的参考数据/);
  assert.match(stale, /来源快照市价（旧报价）/);
  assert.match(stale, /参考来源刷新失败：&lt;offline>/);
  assert.match(stale, /IOPV计算时间<\/dt><dd>来源未提供/);
  assert.doesNotMatch(stale, /参考溢价幅度达到|class="premium-alert"/);
  const zero = premiumHtml({iopv: {...snapshot, reported_reference: {...snapshot.reported_reference, premium_pct: 0}}}, 3, esc, now);
  assert.match(zero, /来源参考折溢价<\/dt><dd>0\.00%/);
});

test('提醒输入框随实时IOPV时效启停，来源参考值不会启用提醒', () => {
  const html = fs.readFileSync(require.resolve('../dashboard/index.html'), 'utf8');
  const now = Date.parse('2026-01-05T02:00:30Z');
  const input = {value: '3', disabled: false, checkValidity: () => true};
  const body = {innerHTML: ''};
  const context = vm.createContext({hasCurrentIopv: snapshot => hasCurrentIopv(snapshot, now),
    premiumHtml: () => 'rendered', escHtml: esc,
    document: {getElementById: id => id === 'premium-threshold' ? input : body}});
  vm.runInContext(html.slice(html.indexOf('function refreshPremiumPanel('), html.indexOf('function loadPremium(')), context);
  context.refreshPremiumPanel({premiumData: {iopv: {status: 'unavailable', reported_reference: {value: 1, premium_pct: 10}}}});
  assert.equal(input.disabled, true);
  const verified = {status: 'ok', snapshot_fresh: true, iopv_fresh: true, iopv: 1, premium_pct: 10,
    iopv_time: '2026-01-05T02:00:00Z', quote_time: '2026-01-05T02:00:00Z', fresh_until: '2026-01-05T02:02:00Z'};
  context.refreshPremiumPanel({premiumData: {iopv: verified}});
  assert.equal(input.disabled, false);
  context.refreshPremiumPanel({premiumData: {iopv: {...verified, fresh_until: '2026-01-05T02:00:01Z'}}});
  assert.equal(input.disabled, true);
  assert.equal(hasCurrentIopv({...verified, iopv_time: '2026-01-05T02:01:00Z'}, now), false);
  assert.equal(hasCurrentIopv(null, now), false);
});

test('小白模式显示后端CAN加权总分，不另算七项平均', () => {
  const html = fs.readFileSync(require.resolve('../dashboard/index.html'), 'utf8');
  const nodes = new Map();
  const context = vm.createContext({C: {up: 'red', down: 'green'}, document: {getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, {innerHTML: ''});
    return nodes.get(id);
  }}});
  const start = html.indexOf('function renderCanslim(');
  const end = html.indexOf('function explainKeyLevel(', start);
  vm.runInContext(html.slice(start, end), context);
  context.renderCanslim({c_score: 10, a_score: 20, n_score: 30, s_score: 40, l_score: 50, i_score: 60, m_score: 70, total: 53, signals: []});
  assert.match(nodes.get('cs-simple').innerHTML, />53<\/span>/);
  assert.doesNotMatch(nodes.get('cs-simple').innerHTML, /基本面综合评分/);
});

function modelFixture() {
  return {status: 'ok', kind: 'model_estimate', label: '模型估算', est_nav: 1.122, est_premium_pct: 6.952,
    est_nav_time: '2026-11-24T02:00:00Z', fresh_until: '2026-11-24T02:02:00Z',
    price: 1.2, price_time: '2026-11-24T02:00:00Z', nav_date: '2026-11-20', base_nav: 1,
    baseline_close_time: '2026-11-20T21:00:00Z', baseline_source: '合成基准', future_symbol: 'NQ',
    nav_source: '合成净值', nav_source_url: 'https://example.test/nav',
    formula: '净值 × 期货价格比 × 汇率价格比', premium_note: '仅作模型参考',
    uncertainty_note: '模型误差尚未校准', notice: true, notice_threshold_pct: 5, alert_eligible: false,
    inputs: {future: {price: 110, time: '2026-11-24T02:00:00Z', source: '合成期货'},
      fx: {price: 7.14, time: '2026-11-24T02:00:00Z', source: '合成汇率'},
      base_future: {price: 100, time: '2026-11-20T21:00:00Z'},
      base_fx: {price: 7, time: '2026-11-20T21:00:00Z'}}};
}

test('模型估算独立展示净值、折溢价和各输入时间，5%只作信息提示', () => {
  const html = premiumHtml({model_estimate: modelFixture()}, 3, esc, Date.parse('2026-11-24T02:00:30Z'));
  assert.match(html, /QDII 模型估算.*非官方IOPV/);
  assert.match(html, /模型估算净值<\/dt><dd>1\.1220/);
  assert.match(html, /估算折溢价<\/dt><dd>\+6\.95%/);
  assert.match(html, /基准净值日期<\/dt><dd>2026-11-20/);
  for (const label of ['期货基准时间', '期货当前报价时间', '汇率基准时间', '汇率当前报价时间']) assert.ok(html.includes(label));
  assert.match(html, /达到 5%，仅作信息提示/);
  assert.doesNotMatch(html, /class="premium-alert"|实时IOPV<\/dt>|参考溢价幅度达到/);
});

test('模型输入过期或缺失时不显示旧估算值，错误文本安全转义', () => {
  const expired = modelEstimateHtml(modelFixture(), esc, Date.parse('2026-11-24T02:02:01Z'));
  assert.match(expired, /估算不可用/);
  assert.doesNotMatch(expired, /1\.1220|6\.95%|达到 5%/);
  const missing = modelEstimateHtml({status: 'unavailable', note: '<缺少基准>', est_nav: null}, esc);
  assert.match(missing, /估算不可用/);
  assert.match(missing, /&lt;缺少基准>/);
  assert.doesNotMatch(missing, /<缺少基准>/);
});
