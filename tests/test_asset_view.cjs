const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {resolveInstrument, seedInstrumentWatchlist, assetSummaryHtml, fundTermsHtml, premiumHtml} = require('../dashboard/asset-view.js');
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
  const data = {iopv: {price: 1.1, iopv: 1, premium_pct: 10, snapshot_fresh: true, alert_eligible: true,
    iopv_fresh: true, iopv_time: '2026-01-05T02:00:00Z',
    quote_time: '2026-01-05T02:00:00Z', fresh_until: '2026-01-05T02:02:00Z', source: '测试', note: 'IOPV无独立时间'},
    disclosed_nav: {value: 0.8, date: '2026-01-02', premium_pct: 37.5, source: '测试', source_url: 'https://example.test', basis: '非盘中估值'}};
  assert.match(premiumHtml(data, 3, esc, now), /参考溢价幅度达到/);
  assert.doesNotMatch(premiumHtml(data, 20, esc, now), /参考溢价幅度达到/);
  assert.doesNotMatch(premiumHtml(data, 3, esc, now + 200000), /参考溢价幅度达到/);
  assert.doesNotMatch(premiumHtml({...data, iopv: null}, 3, esc, now), /参考溢价幅度达到/);
  const unknownTime = premiumHtml({...data, iopv: {...data.iopv, iopv_time: null, iopv_fresh: false}}, 3, esc, now);
  assert.doesNotMatch(unknownTime, /参考溢价幅度达到/);
  assert.match(unknownTime, /IOPV独立更新时间<\/dt><dd>来源未提供/);
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
