const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../dashboard/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../dashboard/fund-view.js'), 'utf8');

function setup() {
  const nodes = new Map();
  const chart = {clear() {this.option = null;}, resize() {}, showLoading() {}, hideLoading() {},
    setOption(option) {this.option = option;}};
  const root = {hidden: false, querySelectorAll: () => [], querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {textContent: '', innerHTML: ''});
    return nodes.get(selector);
  }};
  const context = vm.createContext({module: {exports: {}}, echarts: {init: () => chart}, ResizeObserver: class {observe() {}}});
  vm.runInContext(source, context);
  vm.runInContext(html.slice(html.indexOf('// EMA计算'), html.indexOf('// KDJ计算')), context);
  const view = new context.module.exports.FundView(root, {
    colors: {up: 'red', down: 'green'}, escHtml: value => value,
    calcMACD: context.calcMACD, calcRSI: context.calcRSI,
  });
  const history = ['2026-01-01', '2026-01-02', '2026-01-05', '2026-01-09', '2026-01-12'].map((date, i) => ({
    date, nav: 1 + i / 10, adjusted_nav: 1 + i / 10, change_pct: 1, ma5: null, ma20: null, ma60: null,
  }));
  const data = {history, latest: history.at(-1), source: 'test', analysis: {
    direction: '数据不足', returns: {week: 1, month: null, quarter: null}, max_drawdown_pct: 0,
    ma20: null, ma60: null, start_date: history[0].date, end_date: history.at(-1).date,
  }};
  return {view, chart, nodes, data};
}

test('净值以折线绘制，显示净值日期，缺失收益率不显示为零', () => {
  const {view, chart, nodes, data} = setup();
  view.render(data);
  assert.equal(chart.option.series[0].type, 'line');
  assert.ok(chart.option.series.every(series => series.type !== 'candlestick'));
  assert.match(nodes.get('#fund-status').textContent, /最新净值日 2026-01-12/);
  assert.match(nodes.get('#fund-metrics').innerHTML, /近1月<\/dt><dd class="">--/);
  view.indicator = 'rsi';
  view.renderChart();
  assert.ok(chart.option.series.some(series => series.name === 'RSI14'));
});

test('周净值取各周最后一条已公布记录，不编造周末数据', () => {
  const {view, data, chart} = setup();
  view.render(data);
  view.period = 'week';
  view.renderChart();
  assert.deepEqual(Array.from(chart.option.xAxis[0].data), ['2026-01-02', '2026-01-09', '2026-01-12']);
});

test('重新选基金或读取失败时清除旧图表和旧分析', () => {
  const {view, data, chart, nodes} = setup();
  view.render(data);
  view.reset('https://fund.eastmoney.com/050025.html');
  assert.equal(view.data, null);
  assert.equal(chart.option, null);
  assert.equal(nodes.get('#fund-metrics').textContent, '');
  view.showError('连接超时');
  assert.match(nodes.get('#fund-status').textContent, /连接超时.*刷新净值/);
  assert.equal(nodes.get('#fund-summary').textContent, '暂无可用分析');
});
