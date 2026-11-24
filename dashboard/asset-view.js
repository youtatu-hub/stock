// 品种配置来自后端；目录只用于导航，不决定资产模型。
function resolveInstrument(catalog, symbol) {
  const code = symbol.toLowerCase().replace(/^(sh|sz|bj)/, '');
  if (catalog.instruments[code]) return catalog.instruments[code];
  const assetType = catalog.listed_fund_prefixes.some(prefix => code.startsWith(prefix)) ? 'listed_fund' : 'stock';
  return {code, asset_type: assetType, ...catalog.profiles[assetType]};
}

function seedInstrumentWatchlist(catalog, list, previousVersion, now = Date.now()) {
  const next = list.map(item => {
    // 旧版ETF买卖评分失去适用性，不继续在自选列表显示。
    if (resolveInstrument(catalog, item.code).model === 'stock') return item;
    const {action, score, ...rest} = item;
    return rest;
  });
  const codes = new Set(next.map(item => item.code));
  for (const item of Object.values(catalog.instruments)) {
    if (item.seed_version > previousVersion && !codes.has(item.code)) {
      next.push({code: item.code, name: item.name, addedAt: now});
      codes.add(item.code);
    }
  }
  return next;
}

function assetSummaryHtml(signal, instrument, escHtml) {
  const metrics = signal.asset_metrics;
  const fmt = value => Number.isFinite(value) ? `${value.toFixed(2)}%` : '--';
  return `<div class="sum-action-row"><span class="sum-badge sum-badge-watch">${escHtml(signal.action)}</span>
      <span class="asset-type">${escHtml(instrument.label)}</span></div>
    <div class="sum-text">${escHtml(signal.plain_summary)}</div>
    ${instrument.benchmark ? `<div class="asset-note">跟踪基准：${escHtml(instrument.benchmark)}</div>` : ''}
    <dl class="asset-metrics"><dt>价格趋势</dt><dd>${escHtml(signal.trend?.direction || '数据不足')}</dd>
      <dt>区间价格变化</dt><dd>${fmt(metrics.return_pct)}</dd>
      <dt>区间最大回撤</dt><dd>${fmt(metrics.max_drawdown_pct)}</dd></dl>
    <div class="asset-note">${escHtml(metrics.start_date)} 至 ${escHtml(metrics.end_date)} · ${metrics.sample_count} 根K线。${escHtml(metrics.basis)}</div>
    <div class="asset-note">${escHtml(signal.description)}</div>`;
}

function fundTermsHtml(response, escHtml) {
  const feeLines = rows => rows.length ? rows.map(row => `<div>${escHtml(row.condition)}：${escHtml(row.rate || '未披露')}</div>`).join('') : '未披露';
  const items = [...response.items].sort((a, b) => (a.instrument.share_class || '').localeCompare(b.instrument.share_class || ''));
  return items.map(({instrument, data, error}) => {
    const button = `<button class="fund-share-btn${instrument.code === response.symbol ? ' active' : ''}"
      data-fund-code="${escHtml(instrument.code)}">${escHtml(instrument.share_class || '基金')} · ${escHtml(instrument.code)}</button>`;
    if (!data) return `<section class="fund-terms-share">${button}<div class="asset-error">交易规则获取失败：${escHtml(error || '来源未返回数据')}</div></section>`;
    const trading = data.trading;
    const rows = [['申购', trading.subscription], ['赎回', trading.redemption], ['定投', trading.regular],
      ['日累计申购限额', trading.daily_limit], ['申购起点', trading.minimum],
      ['买入/卖出确认', `${trading.buy_confirmation || '未披露'} / ${trading.sell_confirmation || '未披露'}`],
      ['年销售服务费', data.fees.sales_service], ['年管理费 / 托管费', `${data.fees.management || '未披露'} / ${data.fees.custody || '未披露'}`]];
    return `<section class="fund-terms-share">${button}
      <dl class="asset-metrics">${rows.map(([label, value]) => `<dt>${label}</dt><dd>${escHtml(value || '未披露')}</dd>`).join('')}</dl>
      <details><summary>申购与赎回费率明细</summary><div class="asset-note">申购费（原费率 | 本渠道优惠费率）</div>
        <div class="fee-lines">${feeLines(data.fees.subscription)}</div><div class="asset-note">赎回费</div>
        <div class="fee-lines">${feeLines(data.fees.redemption)}</div></details>
      <div class="asset-note"><a href="${escHtml(data.source_url)}" target="_blank" rel="noopener noreferrer">${escHtml(data.source)} ↗</a>
        · 获取于 ${escHtml(new Date(data.fetched_at).toLocaleString('zh-CN'))}</div></section>`;
  }).join('') + '<div class="asset-note">A/C 应比较申购费、持有期间销售服务费和对应期限赎回费；没有统一的一年分界。规则按销售渠道展示，获取时间不等于生效时间，限额不代表当前开放申购。管理费、托管费及销售服务费已在净值中计提。</div>';
}

function hasCurrentIopv(snapshot, now = Date.now()) {
  return Boolean(snapshot && snapshot.status === 'ok' && snapshot.snapshot_fresh && snapshot.iopv_fresh
    && !snapshot.stale && Number.isFinite(snapshot.iopv) && snapshot.iopv > 0
    && Number.isFinite(snapshot.premium_pct) && now <= Date.parse(snapshot.fresh_until)
    && now >= Date.parse(snapshot.quote_time) && now >= Date.parse(snapshot.iopv_time));
}

function modelEstimateHtml(model, escHtml, now = Date.now()) {
  const number = value => Number.isFinite(value) ? value.toFixed(4) : '--';
  const time = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}) : '来源未提供';
  const valid = model.status === 'ok' && Number.isFinite(model.est_nav) && model.est_nav > 0
    && now >= Date.parse(model.est_nav_time) && now <= Date.parse(model.fresh_until);
  const heading = '<div class="premium-reference-title">QDII 模型估算<span>期货与汇率代理 · 非官方IOPV</span></div>';
  if (!valid) {
    const note = model.status === 'ok' ? '估算输入或比较市价已过期，等待刷新。' : model.note;
    return `<section class="premium-estimate" aria-label="模型估算">${heading}
      <div class="asset-warning">估算不可用</div><div class="asset-note">${escHtml(note || '估算输入不完整')}</div></section>`;
  }
  const inputs = model.inputs;
  const premium = Number.isFinite(model.est_premium_pct)
    ? `${model.est_premium_pct > 0 ? '+' : ''}${model.est_premium_pct.toFixed(2)}%` : '--';
  return `<section class="premium-estimate" aria-label="模型估算">${heading}
    <dl class="asset-metrics"><dt>模型估算净值</dt><dd>${number(model.est_nav)}</dd>
      <dt>估算折溢价</dt><dd>${premium}</dd><dt>比较用市价</dt><dd>${number(model.price)}</dd>
      <dt>估算输入截至</dt><dd>${escHtml(time(model.est_nav_time))}</dd>
      <dt>市价行情时间</dt><dd>${escHtml(time(model.price_time))}</dd>
      <dt>基准净值日期</dt><dd>${escHtml(model.nav_date)}</dd></dl>
    <div class="asset-note">${escHtml(model.premium_note)}</div>
    ${model.notice ? `<div class="model-estimate-note">估算偏离幅度达到 ${escHtml(model.notice_threshold_pct)}%，仅作信息提示，不触发实时提醒。</div>` : ''}
    <details class="estimate-inputs"><summary>估算依据与输入时间</summary><dl class="asset-metrics">
      <dt>基准净值</dt><dd>${number(model.base_nav)}</dd>
      <dt>净值日美股收盘</dt><dd>${escHtml(time(model.baseline_close_time))}</dd>
      <dt>${escHtml(model.future_symbol)}基准价 → 现价</dt><dd>${number(inputs.base_future.price)} → ${number(inputs.future.price)}</dd>
      <dt>期货基准时间</dt><dd>${escHtml(time(inputs.base_future.time))}</dd>
      <dt>期货当前报价时间</dt><dd>${escHtml(time(inputs.future.time))}</dd>
      <dt>USD/CNH基准 → 现价</dt><dd>${number(inputs.base_fx.price)} → ${number(inputs.fx.price)}</dd>
      <dt>汇率基准时间</dt><dd>${escHtml(time(inputs.base_fx.time))}</dd>
      <dt>汇率当前报价时间</dt><dd>${escHtml(time(inputs.fx.time))}</dd></dl>
      <div class="asset-note">${escHtml(model.formula)}。期货使用连续合约最新价，汇率使用离岸人民币即期买卖中值。
        基准来源：${escHtml(model.baseline_source)}；当前报价：${escHtml(inputs.future.source)} / ${escHtml(inputs.fx.source)}。
        <a href="${escHtml(model.nav_source_url)}" target="_blank" rel="noopener noreferrer">${escHtml(model.nav_source)}净值 ↗</a></div></details>
    <div class="asset-note">${escHtml(model.uncertainty_note)}</div></section>`;
}

function premiumHtml(response, threshold, escHtml, now = Date.now()) {
  const snapshot = response.iopv;
  const nav = response.disclosed_nav;
  const number = value => Number.isFinite(value) ? value.toFixed(4) : '--';
  const pct = value => Number.isFinite(value) ? `${value > 0 ? '+' : ''}${value.toFixed(2)}%` : '--';
  const time = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}) : '来源未提供';
  let html = response.model_estimate ? modelEstimateHtml(response.model_estimate, escHtml, now) : '';
  if (snapshot) {
    const fresh = hasCurrentIopv(snapshot, now);
    const reference = snapshot.reported_reference;
    if (fresh) {
      const warning = snapshot.alert_eligible && Number.isFinite(threshold) && threshold > 0 && Math.abs(snapshot.premium_pct) >= threshold;
      html += `<dl class="asset-metrics"><dt>比较用市价</dt><dd>${number(snapshot.price)}</dd>
        <dt>实时IOPV</dt><dd>${number(snapshot.iopv)}</dd><dt>价格相对IOPV折溢价</dt><dd class="${warning ? 'premium-alert' : ''}">${pct(snapshot.premium_pct)}</dd>
        <dt>IOPV估值时间</dt><dd>${escHtml(time(snapshot.iopv_time))}</dd></dl>
        <div class="asset-note">${escHtml(snapshot.source)} · 价格行情时间 ${escHtml(time(snapshot.quote_time))}</div>
        ${warning ? `<div class="asset-warning" role="status">参考${snapshot.premium_pct >= 0 ? '溢价' : '折价'}幅度达到 ${threshold}%，请核对时效与交易成本。</div>` : ''}`;
    } else if (reference && Number.isFinite(reference.value)) {
      const quoteFresh = !snapshot.stale && snapshot.snapshot_fresh && now >= Date.parse(reference.price_time)
        && now <= Date.parse(reference.quote_fresh_until);
      const quoteLabel = now < Date.parse(reference.price_time) ? '（时间异常）' : quoteFresh ? '' : '（旧报价）';
      html += `<section class="premium-reference" aria-label="来源IOPV参考">
        <div class="premium-reference-title">${escHtml(reference.source)} IOPV 参考<span>估值时效未核验</span></div>
        ${snapshot.stale ? '<div class="asset-warning">以下为上次成功获取的参考数据，刷新失败。</div>' : ''}
        <dl class="asset-metrics"><dt>最近收到的IOPV参考值</dt><dd>${number(reference.value)}</dd>
        <dt>来源参考折溢价</dt><dd>${Number.isFinite(reference.premium_pct) ? pct(reference.premium_pct) : '来源未提供'}</dd>
        <dt>来源快照市价${quoteLabel}</dt><dd>${number(reference.source_price)}</dd>
        <dt>行情快照更新时间</dt><dd>${escHtml(time(reference.price_time))}</dd>
        <dt>IOPV计算时间</dt><dd>${escHtml(time(reference.valuation_time))}</dd>
        <dt>本站获取时间</dt><dd>${escHtml(time(reference.fetched_at))}</dd></dl>
        <div class="asset-note">IOPV为来源最近收到的参考值；行情时间和本站获取时间均不代表IOPV计算时间。
          折溢价直接使用来源返回值，时效未核验，不触发实时提醒，也不作为实时IOPV传入AI。
          <a href="${escHtml(reference.source_url)}" target="_blank" rel="noopener noreferrer">${escHtml(reference.source)} ↗</a></div></section>`;
    } else {
      html += `<div class="asset-warning">实时IOPV不可用</div><div class="asset-note">${escHtml(snapshot.status === 'ok' ? '估值或行情已过期，等待刷新。' : snapshot.note)}；不计算当前IOPV折溢价，不触发提醒。</div>`;
    }
    if (snapshot.refresh_error) html += `<div class="asset-error">参考来源刷新失败：${escHtml(snapshot.refresh_error)}</div>`;
  }
  if (nav) html += `<div class="premium-nav"><dl class="asset-metrics"><dt>已披露单位净值</dt><dd>${number(nav.value)}</dd>
    <dt>净值日期</dt><dd>${escHtml(nav.date)}</dd><dt>比较用市价${nav.price_stale ? '（旧报价）' : ''}</dt><dd>${number(nav.price)}</dd>
    <dt>市价行情时间</dt><dd>${escHtml(time(nav.price_time))}</dd><dt>市价相对已披露净值偏离</dt><dd>${pct(nav.premium_pct)}</dd></dl>
    <div class="asset-note">${escHtml(nav.basis)}该口径不触发偏离提醒。
      <a href="${escHtml(nav.source_url)}" target="_blank" rel="noopener noreferrer">${escHtml(nav.source)} ↗</a></div></div>`;
  for (const [key, error] of Object.entries(response.errors || {})) {
    if (key === 'estimate_market' && response.model_estimate) continue;
    html += `<div class="asset-error">${{iopv:'IOPV参考值',nav:'已披露净值',quote:'市价'}[key] || escHtml(key)}获取失败：${escHtml(error)}</div>`;
  }
  return html;
}

if (typeof module !== 'undefined') module.exports = {
  resolveInstrument, seedInstrumentWatchlist, assetSummaryHtml, fundTermsHtml, hasCurrentIopv, modelEstimateHtml, premiumHtml,
};
