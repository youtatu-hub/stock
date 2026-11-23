const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {renderAiContent} = require('../dashboard/ai-view.js');

function setup() {
  const nodes = new Map(), timers = new Map();
  let timerId = 0;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'', textContent:'', innerHTML:'', style:{}, dataset:{}, children:[], hidden:false, listeners:{},
      addEventListener(event, callback){this.listeners[event]=callback;}, setAttribute(){}, prepend(){},
      replaceChildren(){this.children=[];this.value='';},
      appendChild(child){this.children.push(child);if(!this.value)this.value=child.value;},
      querySelector: key=>node(id+key)});
    return nodes.get(id);
  };
  const requests = [], saved = new Map();
  class Session {
    active = true;
    cancel(){this.active=false;}
    request(url, options){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}
  }
  const context = vm.createContext({console, StockLoadSession:Session,
    localStorage:{setItem:(key,value)=>saved.set(key,value),getItem:key=>saved.get(key),removeItem:key=>saved.delete(key)},
    setTimeout:callback=>{timers.set(++timerId,callback);return timerId;},
    clearTimeout:id=>timers.delete(id),
    document:{createElement:()=>({value:'',textContent:''}), getElementById:node, querySelector:node},
    root:node('ai-card'),
  });
  context.root.querySelector=node;
  vm.runInContext(fs.readFileSync(require.resolve('../dashboard/ai-view.js'),'utf8')+'\nvar panel = new AIAnalysisPanel(root);',context);
  return {nodes,node,requests,saved,context,panel:context.panel,timers,
    flush(){const pending=[...timers.values()];timers.clear();pending.forEach(callback=>callback());}};
}
const instrument=code=>({code,name:'测试品种',model:'technical'});
const payload=(symbol,text='当前品种的解读')=>({symbol,period:'day',model:'alpha',text,analyzed_at:new Date().toISOString()});

test('只取模型列表，迁移后浏览器不保存连接信息，并自动生成解读',async()=>{
  const page=setup(), panel=page.panel;
  page.saved.set('stock_cpa_settings',JSON.stringify({base_url:'https://private.example/v1',model:'beta'}));
  const initialization=panel.initialize();
  panel.setTarget(instrument('513300'),'day');
  assert.equal(page.requests[0].url,'/api/ai/models');
  assert.equal(page.requests[0].options.body,'{}');
  page.requests[0].resolve({models:['alpha','beta'],default_model:'alpha'});
  await initialization;
  assert.equal(panel.model.value,'beta');
  assert.equal(page.saved.has('stock_cpa_settings'),false);
  assert.equal(page.saved.get('stock_cpa_model'),'beta');
  page.flush();
  assert.deepEqual(JSON.parse(page.requests[1].options.body),{model:'beta',symbol:'513300',period:'day'});
  assert.doesNotMatch([...page.saved.values()].join(''),/private.example|api_key|base_url/);
  page.requests[1].resolve(payload('513300'));
  await new Promise(setImmediate);
  assert.equal(panel.root.dataset.state,'ready');
  assert.match(panel.output.innerHTML,/<p>当前品种的解读<\/p>/);
  panel.setTarget(instrument('513300'),'day');
  page.flush();
  assert.equal(page.requests.length,2);
});

test('快速切换只请求最后品种，迟到的旧回复不覆盖新解读',async()=>{
  const page=setup(), panel=page.panel;
  panel.modelsReady=true;panel.model.value='alpha';
  panel.setTarget(instrument('513300'),'day');
  panel.setTarget(instrument('159941'),'week');
  page.flush();
  assert.equal(page.requests.length,1);
  assert.deepEqual(JSON.parse(page.requests[0].options.body),{model:'alpha',symbol:'159941',period:'week'});
  panel.setTarget(instrument('513500'),'day');
  page.flush();
  page.requests[0].resolve(payload('159941','旧品种'));
  await new Promise(setImmediate);
  assert.doesNotMatch(panel.output.innerHTML,/旧品种/);
  page.requests[1].resolve(payload('513500','<script>仅作文本显示</script>'));
  await new Promise(setImmediate);
  assert.match(panel.output.innerHTML,/&lt;script&gt;仅作文本显示&lt;\/script&gt;/);
  assert.doesNotMatch(panel.output.innerHTML,/<script>/);
});

test('模型切换后自动生成，快速切回原模型也不会丢失请求',async()=>{
  const page=setup(), panel=page.panel;
  panel.modelsReady=true;panel.model.value='alpha';
  panel.setTarget(instrument('513300'),'day');
  page.flush();
  panel.model.value='beta';panel.model.listeners.change();
  panel.model.value='alpha';panel.model.listeners.change();
  page.flush();
  assert.equal(page.requests.length,2);
  assert.equal(JSON.parse(page.requests[1].options.body).model,'alpha');
  page.requests[0].resolve(payload('513300','已取消的回复'));
  page.requests[1].resolve(payload('513300','当前回复'));
  await new Promise(setImmediate);
  assert.match(panel.output.innerHTML,/<p>当前回复<\/p>/);
});

test('刷新列表后保留有效选择并更新解读，失败可明确重试',async()=>{
  const page=setup(), panel=page.panel;
  panel.model.value='beta';panel.modelsReady=true;
  panel.setTarget(instrument('513300'),'day');
  const refreshing=panel.refreshModels();
  page.requests[0].resolve({models:['alpha','beta'],default_model:'alpha'});
  await refreshing;
  page.flush();
  assert.equal(panel.model.value,'beta');
  page.requests[1].reject(new Error('CPA响应超时'));
  await new Promise(setImmediate);
  assert.equal(panel.root.dataset.state,'error');
  assert.match(panel.output.textContent,/CPA响应超时.*刷新模型/);
  const retry=panel.refreshModels();
  page.requests[2].resolve({models:['alpha'],default_model:'alpha'});
  await retry;
  page.flush();
  assert.equal(JSON.parse(page.requests[3].options.body).model,'alpha');
  assert.equal(panel.refreshButton.disabled,false);
});

test('等待页面快照后生成，普通金额刷新不重复调用，状态改变重新解读',async()=>{
  const page=setup(), panel=page.panel;
  panel.modelsReady=true;panel.model.value='alpha';
  panel.setTarget({...instrument('513300'),fund_flow:true},'day');
  page.flush();
  assert.equal(page.requests.length,0);
  assert.match(panel.status.textContent,/等待页面资金流/);
  const flow={snapshot_id:'a'.repeat(32),status:'partial',summary_available:true,minute_status:'partial',source:'腾讯',
    data_date:'2026-11-23',as_of:'2026-11-23 15:00',summary:{main_net:-2000},
    availability_note:'实时累计可用；分钟序列局部缺失'};
  panel.setFlowSnapshot('159941',flow);
  page.flush();
  assert.equal(page.requests.length,0);
  panel.setFlowSnapshot('513300',flow);
  page.flush();
  assert.deepEqual(JSON.parse(page.requests[0].options.body).flow_snapshot,{id:flow.snapshot_id,status:'partial'});
  const newer={...flow,snapshot_id:'b'.repeat(32),summary:{main_net:-1000}};
  panel.setFlowSnapshot('513300',newer);
  page.flush();
  assert.equal(page.requests.length,1);
  page.requests[0].resolve({...payload('513300'),fund_flow:{realtime:flow,daily_history:{availability_note:'日级历史未请求'}}});
  await new Promise(setImmediate);
  assert.match(panel.dataState.textContent,/实时累计可用；分钟序列局部缺失；日级历史未请求；资金流截至 2026-11-23 15:00 · 腾讯/);
  panel.setFlowSnapshot('513300',{...newer,status:'stale',stale:true});
  page.flush();
  assert.deepEqual(JSON.parse(page.requests[1].options.body).flow_snapshot,{id:newer.snapshot_id,status:'stale'});
  panel.setFlowSnapshot('513300',newer);
  page.flush();
  assert.equal(page.requests.length,3);
  assert.equal(JSON.parse(page.requests[2].options.body).flow_snapshot.status,'partial');
});

test('日级模式不等待实时渲染；实时获取失败也把页面不可用状态交给模型',()=>{
  const page=setup(), panel=page.panel;
  panel.modelsReady=true;panel.model.value='alpha';
  panel.setTarget({...instrument('513300'),fund_flow:true},'day','daily');
  page.flush();
  assert.equal(page.requests.length,1);
  assert.equal('flow_snapshot' in JSON.parse(page.requests[0].options.body),false);
  panel.setFlowMode('realtime');
  page.flush();
  assert.equal(page.requests.length,1);
  panel.setFlowSnapshot('513300',{status:'error',flows:[]});
  page.flush();
  assert.deepEqual(JSON.parse(page.requests[1].options.body).flow_snapshot,{id:null,status:'error'});
  panel.setFlowMode('daily');
  page.flush();
  assert.equal(page.requests.length,3);
  assert.equal('flow_snapshot' in JSON.parse(page.requests[2].options.body),false);
});

test('CPA卡片仅提供模型选择和刷新按钮',()=>{
  const html=fs.readFileSync(require.resolve('../dashboard/index.html'),'utf8');
  const card=html.slice(html.indexOf('id="ai-card"'),html.indexOf('id="premium-card"'));
  assert.equal((card.match(/<button/g)||[]).length,1);
  assert.match(card,/data-ai-refresh/);
  assert.match(card,/<select name="model"/);
  assert.doesNotMatch(card,/api_key|base_url|<input|data-ai-analyze|<details/);
});

test('同截图的加粗管线标题转为分区，保留正文与强调且不露出Markdown符号',()=>{
  const html=renderAiContent('**结论 | 示例ETF：回撤后等待确认。** 截至数据日，趋势参考。\n\n**依据 | ** 价格仍在 **MA20上方**。\n\n**风险 | ** 注意波动。\n\n**观察条件 | ** 等待放量。');
  assert.equal((html.match(/<section /g)||[]).length,4);
  assert.match(html,/ai-insight-conclusion/);
  assert.match(html,/示例ETF：回撤后等待确认。 截至数据日，趋势参考。/);
  assert.match(html,/<strong>MA20上方<\/strong>/);
  assert.match(html,/ai-insight-risk/);
  assert.doesNotMatch(html,/\*\*|结论 \|/);
});

test('标准Markdown标题和列表正确分段，外部HTML只显示为文本',()=>{
  const html=renderAiContent('## 结论\n趋势向上。\n\n## 依据\n- **量价配合**\n- 关注 `MA20`\n\n## 风险\n<img src=x onerror=alert(1)>\n\n## 观察条件\n1. 确认突破\n2. 控制回撤');
  assert.match(html,/<ul><li><strong>量价配合<\/strong><\/li>/);
  assert.match(html,/<code>MA20<\/code>/);
  assert.match(html,/<ol><li>确认突破<\/li><li>控制回撤<\/li><\/ol>/);
  assert.match(html,/&lt;img/);
  assert.doesNotMatch(html,/<img|<script|<iframe/);
});

test('511090区块、核心CAN和股票CAN按服务端能力恢复，五只美股不隐藏AI',()=>{
  const page=setup();
  const html=fs.readFileSync(require.resolve('../dashboard/index.html'),'utf8');
  vm.runInContext(html.slice(html.indexOf('function applyAssetVisibility('),html.indexOf('function refreshPremiumPanel(')),page.context);
  const cases=[
    {model:'defensive',asset_type:'long_bond_etf',price_models:true,fund_flow:true,canslim:false},
    {model:'technical',asset_type:'equity_etf',price_models:true,fund_flow:true,canslim:true},
    {model:'stock',asset_type:'stock',price_models:true,fund_flow:true,canslim:true},
    {model:'defensive',asset_type:'short_bond_etf',price_models:false,fund_flow:true,canslim:false},
    ...['513500','513300','159501','159941','513100'].map(code=>({code,model:'technical',asset_type:'overseas_etf',price_models:true,fund_flow:true,canslim:false})),
  ];
  for(const item of cases){
    page.context.applyAssetVisibility(item);
    assert.equal(page.node('[data-card="canslim"]').hidden,!item.canslim);
    assert.equal(page.node('[data-card="signals"]').hidden,!item.price_models);
    assert.equal(page.node('flow-section').hidden,false);
    assert.equal(page.node('ai-card').hidden,false);
  }
});
