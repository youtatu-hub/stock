const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(require('node:path').join(__dirname, '../dashboard/index.html'), 'utf8');
const source = html.slice(html.indexOf('function renderFlow(flows,'), html.indexOf('// ===== 一句话总结'));

function setup() {
  const nodes = new Map(), pending = [], timers = new Map(), aiSnapshots = [], aiModes = [];
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
    aiPanel:{setFlowSnapshot:(symbol,data)=>aiSnapshots.push({symbol,data}),setFlowMode:mode=>aiModes.push(mode)},
    escHtml:value=>String(value), formatDataTime:value=>value || '未知',
    document:{getElementById(id) {if(!nodes.has(id)) nodes.set(id,{textContent:'',innerHTML:'',open:false,hidden:true,scrollTop:0,classList:{toggle(){}}});return nodes.get(id);}},
    setTimeout:(callback, delay)=>{timers.set(++timerId,{callback,delay});return timerId;},
    clearTimeout:id=>timers.delete(id),
  });
  vm.runInContext(source, context);
  return {run:code=>vm.runInContext(code, context), nodes, pending, timers, chart, aiSnapshots, aiModes};
}

test('切回实时立即清除旧图；失败显示时间和重试，并独立安排下次请求', async () => {
  const page = setup();
  page.run("switchFlowMode('realtime')");
  assert.equal(page.nodes.get('flow-heading').textContent, '资金流向 · 今日实时');
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
  assert.match(page.nodes.get('flow-details-rows').innerHTML, /2026-11-12.*-0.50.*-0.30/);
});

test('实时图显示实际来源和统计口径，切源时整组替换', () => {
  const page = setup();
  page.run(`renderRealtimeFlow({source:'腾讯', source_note:'按腾讯口径统计，不同来源的资金流金额可能有差异',
    data_date:'2026-11-16', as_of:'2026-11-16 15:00', fetched_at:'2026-11-16T07:00:01Z',
    flows:[{time:'09:31', main_net:5000, super_large_net:3000},{time:'15:00', main_net:10000, super_large_net:6000}],
    summary:{main_net:10000, super_large_net:6000}})`);
  assert.match(page.nodes.get('flow-meta').innerHTML, /腾讯.*按腾讯口径统计/);
  assert.equal(page.chart.option.series[0].data[1].value, 0.0001);
  page.run(`renderRealtimeFlow({source:'东方财富', data_date:'2026-11-16',
    flows:[{time:'15:00', main_net:-10000, super_large_net:-6000}],
    summary:{main_net:-10000, super_large_net:-6000}})`);
  assert.match(page.nodes.get('flow-meta').innerHTML, /东方财富/);
  assert.equal(page.chart.option.series[0].data.length, 1);
  assert.equal(page.chart.option.series[0].data[0].value, -0.0001);
});

test('首小时缺口显示为空，保留有效累计值，并把同一快照交给AI', () => {
  const page = setup();
  page.run(`renderRealtimeFlow({snapshot_id:'a'.repeat(32),status:'partial',source:'腾讯',summary_available:true,
    minute_status:'partial',availability_note:'实时累计可用；分钟序列局部缺失',
    missing_ranges:[{start:'09:31',end:'10:29',count:59}],
    flows:[{time:'09:31',available:false,main_net:null,super_large_net:null,large_net:null,medium_net:null,small_net:null},
      {time:'15:00',available:true,main_net:-20000000,super_large_net:-30000000,large_net:10000000,medium_net:5000000,small_net:15000000}],
    summary:{main_net:-20000000,super_large_net:-30000000,large_net:10000000,medium_net:5000000,small_net:15000000}})`);
  assert.match(page.nodes.get('flow-meta').innerHTML, /实时累计可用；分钟序列局部缺失.*09:31–10:29（59分钟）/);
  assert.match(page.nodes.get('flow-summary').innerHTML, /-0\.20亿/);
  assert.equal(page.chart.option.series[0].data[0].value, null);
  assert.equal(page.chart.option.series[0].data[1].value, -0.2);
  for (const series of page.chart.option.series.slice(1)) assert.equal(series.data[0], null);
  assert.match(page.nodes.get('flow-details-rows').innerHTML, /09:31.*未提供/);
  assert.equal(page.aiSnapshots[0].symbol, '999999');
  assert.equal(page.aiSnapshots[0].data.snapshot_id, 'a'.repeat(32));
  assert.equal(page.aiSnapshots[0].data.summary.main_net, -20000000);
});

test('刷新失败保留原快照编号、金额和时间，AI收到的也是旧快照状态', async () => {
  const page = setup();
  page.run("switchFlowMode('realtime')");
  const good = {snapshot_id:'a'.repeat(32),status:'ok',source:'腾讯',summary_available:true,minute_status:'complete',
    fetched_at:new Date().toISOString(),as_of:'2026-11-23 15:00',availability_note:'实时累计可用；分钟序列完整',
    flows:[{time:'15:00',main_net:10000}],summary:{main_net:10000}};
  page.pending[0].resolve(good);
  await new Promise(setImmediate);
  [...page.timers.values()][0].callback();
  page.pending[1].resolve({status:'error',flows:[],message:'请求超时'});
  await new Promise(setImmediate);
  const stale = page.aiSnapshots.at(-1).data;
  assert.equal(stale.snapshot_id, good.snapshot_id);
  assert.equal(stale.status, 'stale');
  assert.equal(stale.summary.main_net, good.summary.main_net);
  assert.equal(stale.as_of, good.as_of);
  assert.match(page.nodes.get('flow-summary').innerHTML, /刷新失败，显示旧数据/);
  assert.doesNotMatch(page.nodes.get('flow-meta').innerHTML, /实时累计可用/);
  assert.deepEqual(page.aiModes, ['realtime']);
});

test('盘前说明、五类资金和分时明细同时展示', () => {
  const page = setup();
  page.run(`renderRealtimeFlow({source:'腾讯', data_date:'2026-11-16', is_today:false,
    session_note:'今日尚未进入连续交易，显示最近可用交易日 2026-11-16 的数据',
    flows:[{time:'15:00', main_net:6000000,super_large_net:-683000000,large_net:689000000,medium_net:-2000000,small_net:-4000000}],
    summary:{main_net:6000000,super_large_net:-683000000,large_net:689000000,medium_net:-2000000,small_net:-4000000}})`);
  assert.match(page.nodes.get('flow-meta').innerHTML, /今日尚未进入连续交易/);
  assert.match(page.nodes.get('flow-meta').innerHTML, /主力净额 = 超大单净额 \+ 大单净额/);
  assert.match(page.nodes.get('flow-summary').innerHTML, /大单.*\+6.89亿.*中单.*小单/);
  assert.match(page.nodes.get('flow-details-title').textContent, /分时累计明细/);
  assert.match(page.nodes.get('flow-details-rows').innerHTML, /15:00.*68900\.00/s);
  assert.equal(page.chart.option.series.length, 5);
});

test('展开分时明细后自动刷新保留展开和滚动位置，表内数值继续更新', async () => {
  const page = setup();
  page.run("switchFlowMode('realtime')");
  const payload = net => ({status:'ok',source:'腾讯',data_date:'2026-11-17',is_today:true,
    fetched_at:new Date().toISOString(),retry_after_seconds:10,
    flows:[{time:'10:15',main_net:net}],summary:{main_net:net}});
  page.pending[0].resolve(payload(10000));
  await new Promise(setImmediate);
  const disclosure = page.nodes.get('flow-details-toggle');
  disclosure.open = true;
  page.nodes.get('flow-details-scroll').scrollTop = 60;
  [...page.timers.values()][0].callback();
  page.pending[1].resolve(payload(20000));
  await new Promise(setImmediate);
  assert.equal(page.nodes.get('flow-details-toggle'), disclosure);
  assert.equal(disclosure.open, true);
  assert.equal(page.nodes.get('flow-details-scroll').scrollTop, 60);
  assert.equal(page.nodes.get('flow-details').hidden, false);
  assert.match(page.nodes.get('flow-details-rows').innerHTML, /10:15.*2\.00/);
  page.run("switchFlowMode('daily')");
  assert.equal(page.nodes.get('flow-heading').textContent, '资金流向 · 近30日');
  assert.equal(disclosure.open, false);
});

test('日级分析刷新保留明细展开，暂时无记录后恢复也不重建折叠控件', () => {
  const page = setup();
  page.run("resetFlowDetails(); renderFlowDetails([{date:'2026-11-16',main_net:10000}])");
  page.nodes.get('flow-details-toggle').open = true;
  page.run("_renderDailyFlow([{date:'2026-11-16',main_net:20000}])");
  assert.equal(page.nodes.get('flow-details-toggle').open, true);
  page.run('renderFlowDetails([])');
  assert.equal(page.nodes.get('flow-details').hidden, true);
  page.run("renderFlowDetails([{date:'2026-11-16',main_net:30000}])");
  assert.equal(page.nodes.get('flow-details-toggle').open, true);
  assert.match(page.nodes.get('flow-details-rows').innerHTML, /3\.00/);
});

test('分析刷新清空旧风险；报价恢复后历史报价故障不再作为当前故障', () => {
  const nodes = new Map();
  const context = vm.createContext({
    document: {getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, {textContent:'20:26旧错误',style:{display:'block'}});
      return nodes.get(id);
    }}, formatDataTime: value => value,
  });
  vm.runInContext(html.slice(html.indexOf('function setStageStatus('), html.indexOf('async function analyze(')), context);
  vm.runInContext('clearAnalysisPanels()', context);
  assert.equal(nodes.get('risk-list').textContent, '');
  assert.equal(nodes.get('market-body').textContent, '');
  assert.equal(nodes.get('risk-card').style.display, 'none');
  vm.runInContext(`renderAnalysisStatus({quoteReady:true,analysisData:{analyzed_at:'09:35',degraded:{quote:'数据源返回空结果'}}})`, context);
  assert.equal(nodes.get('status-analysis').textContent, '分析：已完成 09:35');
});

test('过期中枢只显示在历史区域，不作为最近有效中枢', () => {
  const nodes = new Map();
  const context = vm.createContext({currentView:'dayk',escHtml: value => value,
    document:{getElementById(id) {if(!nodes.has(id)) nodes.set(id,{style:{},innerHTML:''}); return nodes.get(id);}},
  });
  vm.runInContext(html.slice(html.indexOf('function renderChanlunDaily('), html.indexOf('// ===== 日K线缠论图表叠加')), context);
  vm.runInContext(`renderChanlunDaily({zhongshus:[{zd:1.199,zg:1.213,expired:true}],latest_zhongshu:null,
    expired_zhongshus:[{zd:1.199,zg:1.213,end_date:'2026-10-15',expiry_reason:'偏离现价17.66%'}]})`, context);
  const rendered = nodes.get('chanlun-daily-body').innerHTML;
  assert.match(rendered, /暂无有效中枢/);
  assert.match(rendered, /已过期中枢.*偏离现价17.66%/);
  assert.doesNotMatch(rendered, /最近中枢 <b>/);
});

test('周期刷新替换缠论叠加层，并清除已过期的中枢矩形', () => {
  let series = {type:'candlestick',name:'K线',markPoint:{data:[{name:'止损'}]},markLine:{data:[]}};
  const context = vm.createContext({_klineData:[],_signalPoints:[],C:{up:'red',down:'green'},
    klineChart:{getOption:()=>({series:[series]}),setOption:option=>{series={...series,...option.series[0]};}},
  });
  vm.runInContext(html.slice(html.indexOf('function applyChanlunDailyOverlay('), html.indexOf('function renderMinute(')), context);
  const payload = {chart_signals:[{symbol:'triangle',coord:['2026-11-16',1.06]}],
    chart_strokes:[{coords:[['2026-11-12',1],['2026-11-16',1.06]]}],
    chart_zhongshus:[{xAxis:['2026-11-12','2026-11-16'],yAxis:[1,1.01]}]};
  vm.runInContext(`applyChanlunDailyOverlay(${JSON.stringify(payload)})`, context);
  vm.runInContext(`applyChanlunDailyOverlay(${JSON.stringify(payload)})`, context);
  assert.equal(series.markPoint.data.length, 2);
  assert.equal(series.markLine.data.length, 1);
  vm.runInContext('applyChanlunDailyOverlay({})', context);
  assert.equal(series.markArea.data.length, 0);
  assert.equal(series.markPoint.data.length, 1);
  assert.equal(series.markLine.data.length, 0);
});
