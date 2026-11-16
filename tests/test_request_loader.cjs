const { test } = require('node:test');
const assert = require('node:assert/strict');
const { StockLoadSession } = require('../dashboard/request-loader.js');

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}
const response = data => ({ ok: true, json: async () => data });

test('报价和 K 线先显示，慢分析失败不影响已展示内容', async () => {
  const analysis = deferred();
  const original = global.fetch;
  global.fetch = async url => url === 'analysis' ? analysis.promise : response({ stage: url });
  const seen = [];
  const session = new StockLoadSession();
  try {
    const slow = session.run('analysis', 'analysis', () => seen.push('analysis'), () => seen.push('analysis failed'));
    await Promise.all(['quote', 'kline'].map(stage => session.run(stage, stage, () => seen.push(stage), assert.fail)));
    assert.deepEqual(seen, ['quote', 'kline']);
    analysis.resolve(response({ error: 'upstream timeout' }));
    // 分析错误不撤销已展示的报价和 K 线。
    await slow;
    assert.deepEqual(seen, ['quote', 'kline', 'analysis failed']);
  } finally {
    global.fetch = original;
    session.cancel();
  }
});

test('旧股票晚返回不会覆盖新股票', async () => {
  const late = deferred();
  const original = global.fetch;
  global.fetch = () => late.promise;
  const seen = [];
  const session = new StockLoadSession();
  try {
    const pending = session.run('old', 'old', () => seen.push('old'), () => seen.push('error'));
    session.cancel();
    late.resolve(response({ symbol: 'old' }));
    await pending;
    assert.deepEqual(seen, []);
  } finally {
    global.fetch = original;
  }
});

test('挂起请求到期后给出可见错误', async () => {
  const original = global.fetch;
  global.fetch = (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  const session = new StockLoadSession(20);
  try {
    await assert.rejects(session.request('slow'), /请求超时/);
  } finally {
    session.cancel();
    global.fetch = original;
  }
});
