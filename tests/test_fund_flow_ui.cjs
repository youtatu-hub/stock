const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(require('node:path').join(__dirname, '../dashboard/index.html'), 'utf8');
const source = html.slice(html.indexOf('function renderFlow(flows,'), html.indexOf('// ===== 一句话总结'));

function setup() {
  const nodes = new Map(), pending = [], timers = new Map();
  const chart = {option: {old: true}, clear() { this.option = null; }, setOption(value) {this.option = value;}};
  let timerId = 0;
  class Request {
    constructor(timeout) {this.timeout = timeout;}
    cancel() {}
    request(url) {
      return new Promise(resolve => pending.push({resolve, url, timeout: this.timeout}));
    }
  }
  const context = vm.createContext({
    _dailyFlows: null, _dailyFlowStatus: {}, _flowMode: 'daily', _flowGeneration: 0,
    _flowTimer: null, _flowRequestSession: null, _loadSession: {active: true},
    _klineData: [{date:'2026-11-12'}, {date:'2026-11-13'}, {date:'2026-11-16'}],
    currentSymbol:'999999', currentView:'dayk', API:'', flowChart:chart,
    StockLoadSession:Request, C:{up:'red',down:'green',textDim:'#888'},
    escHtml:value=>String(value), formatDataTime:value=>value || '未知',
    document:{getElementById(id) {if(!nodes.has(id)) nodes.set(id,{textContent:'',innerHTML:'',classList:{toggle(){}}});return nodes.get(id);}},
    setTimeout:(callback, delay)=>{timers.set(++timerId,{callback,delay});return timerId;},
    clearTimeout:id=>timers.delete(id),
  });
  vm.runInContext(source, context);
  return {run:code=>vm.runInContext(code, context), nodes, pending, timers, chart};
}

test('切回实时立即清除旧图；失败显示时间和重试，并独立安排下次请求', async () => {
  const page = setup();
  page.run("switchFlowMode('realtime')");
  assert.equal(page.chart.option.title.text, '正在获取分时资金流…');
  assert.equal(page.pending[0].timeout, 10000);
  page.pending[0].resolve({status:'error',message:'连接超时',flows:[],checked_at:'2026-11-16T13:00:00Z',retry_after_seconds:30});
  await new Promise(setImmediate);
  assert.equal(page.nodes.get('flow-retry').disabled, false);
  assert.match(page.nodes.get('flow-meta').textContent, /本次检查.*下次自动尝试/);
  const timer = [...page.timers.values()][0];
  assert.equal(timer.delay, 30000);
  timer.callback();
  assert.equal(page.pending.length, 2);
});

test('切换后迟到的历史响应不会覆盖实时面板', async () => {
  const page = setup();
  const history = page.run("loadHistoricalFlow('999999')");
  page.run("switchFlowMode('realtime')");
  page.pending[0].resolve({flows:[{date:'2026-11-12',main_net:100,super_large_net:100}],flow_status:{}});
  await history;
  assert.equal(page.chart.option.title.text, '正在获取分时资金流…');
  page.pending[1].resolve({status:'unavailable',flows:[],checked_at:'2026-11-16T13:00:00Z'});
  await new Promise(setImmediate);
  assert.equal(page.chart.option.title.text, '分时资金流暂无记录');
});

test('日级合计带日期范围，展示滞后交易日、获取时间和逐日数值', () => {
  const page = setup();
  page.run("_renderDailyFlow([{date:'2026-11-11',main_net:20000,super_large_net:10000},{date:'2026-11-12',main_net:-5000,super_large_net:-3000}],{fetched_at:'2026-11-16T13:00:00Z'})");
  assert.match(page.nodes.get('flow-summary').innerHTML, /最近2个资金流交易日.*2026-11-11~2026-11-12/);
  assert.match(page.nodes.get('flow-meta').textContent, /截至 2026-11-12.*获取.*缺少最近2个交易日/);
  assert.match(page.nodes.get('flow-details').innerHTML, /2026-11-12.*-0.50.*-0.30/);
});
