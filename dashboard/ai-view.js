const AI_SECTION_STYLES = {结论: 'conclusion', 依据: 'evidence', 风险: 'risk', 观察条件: 'conditions'};

function aiInlineHtml(text) {
  // 先转义模型文本，仅生成限定的排版标签，不接收模型提供的HTML。
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
    .replace(/`([^`\n]+)`/g, '<code>$1</code>');
}

function aiBlocksHtml(lines) {
  const blocks = [];
  let paragraph = [], items = [], listType = '';
  const flushParagraph = () => {
    if (paragraph.length) blocks.push(`<p>${aiInlineHtml(paragraph.join(' '))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (items.length) blocks.push(`<${listType}>${items.map(item => `<li>${aiInlineHtml(item)}</li>`).join('')}</${listType}>`);
    items = [];
    listType = '';
  };
  for (const line of lines) {
    if (!line) { flushParagraph(); flushList(); continue; }
    const item = line.match(/^(?:([-+*])\s+|\d+[.、)]\s+)(.+)$/);
    if (item) {
      flushParagraph();
      const type = item[1] ? 'ul' : 'ol';
      if (listType && listType !== type) flushList();
      listType = type;
      items.push(item[2]);
      continue;
    }
    flushList();
    const heading = line.match(/^#{1,6}\s+(.+?)\s*#*$/);
    if (heading) {
      flushParagraph();
      blocks.push(`<h4>${aiInlineHtml(heading[1])}</h4>`);
    } else {
      paragraph.push(line);
    }
  }
  flushParagraph();
  flushList();
  return blocks.join('');
}

function renderAiContent(text) {
  const sections = [];
  let current = {title: '', lines: []};
  for (const raw of text.replace(/\r\n?/g, '\n').trim().split('\n')) {
    const line = raw.trim();
    const heading = line.match(/^(?:#{1,6}\s+)?(?:\d+[.、)]\s+)?(\*\*|__)?(结论|依据|风险|观察条件)(?=$|[\s:：|｜*_])(.*)$/);
    if (!heading) { current.lines.push(line); continue; }
    if (current.title || current.lines.some(Boolean)) sections.push(current);
    let remainder = heading[3].trim();
    // 兼容“**结论**：正文”和截图中的“**结论 | 正文**”。
    if (heading[1]) remainder = remainder.replace(heading[1], '');
    remainder = remainder.trim().replace(/^[:：|｜]\s*/, '');
    current = {title: heading[2], lines: remainder ? [remainder] : []};
  }
  if (current.title || current.lines.some(Boolean)) sections.push(current);
  return sections.map(section => {
    const style = AI_SECTION_STYLES[section.title] || 'plain';
    const title = section.title ? `<h3 class="ai-section-title">${section.title}</h3>` : '';
    return `<section class="ai-insight ai-insight-${style}">${title}<div class="ai-section-content">${aiBlocksHtml(section.lines)}</div></section>`;
  }).join('');
}

// 浏览器只保存模型偏好；CPA地址与密钥由后端管理。
class AIAnalysisPanel {
  constructor(root, api = '') {
    this.root = root;
    this.api = api;
    this.model = root.querySelector('[name="model"]');
    this.status = root.querySelector('[data-ai-status]');
    this.output = root.querySelector('[data-ai-output]');
    this.context = root.querySelector('[data-ai-context]');
    this.time = root.querySelector('[data-ai-time]');
    this.dataState = root.querySelector('[data-ai-data-state]');
    this.refreshButton = root.querySelector('[data-ai-refresh]');
    this.modelsReady = false;
    this.lastKey = '';
    this.refreshButton.addEventListener('click', () => this.refreshModels());
    this.model.addEventListener('change', () => {
      this.remember();
      this.scheduleAnalysis();
    });
  }

  async initialize() {
    try {
      const legacy = JSON.parse(localStorage.getItem('stock_cpa_settings') || '{}');
      this.preferredModel = localStorage.getItem('stock_cpa_model') || legacy.model || '';
      localStorage.removeItem('stock_cpa_settings');
    } catch (error) {
      this.storageNote = '模型偏好读取失败，将使用服务端默认模型。';
    }
    return this.refreshModels();
  }

  setState(state, message) {
    this.root.dataset.state = state;
    this.root.setAttribute('aria-busy', String(state === 'loading' || state === 'analyzing'));
    this.status.textContent = [message, this.storageNote].filter(Boolean).join(' ');
  }

  remember() {
    this.preferredModel = this.model.value;
    try {
      localStorage.setItem('stock_cpa_model', this.preferredModel);
      this.storageNote = '';
    } catch (error) {
      this.storageNote = '浏览器未能记住模型，本次选择仍可使用。';
    }
  }

  setModels(models, selected) {
    this.model.replaceChildren();
    for (const id of models) {
      const option = document.createElement('option');
      option.value = id;
      option.textContent = id;
      this.model.appendChild(option);
    }
    if (models.includes(selected)) this.model.value = selected;
  }

  cancelAnalysis() {
    clearTimeout(this.timer);
    this.timer = null;
    this.request?.cancel();
    this.request = null;
    this.lastKey = '';
  }

  async refreshModels() {
    this.connection?.cancel();
    this.cancelAnalysis();
    const request = this.connection = new StockLoadSession(15000);
    const selected = this.model.value || this.preferredModel;
    this.modelsReady = false;
    this.model.disabled = true;
    this.refreshButton.disabled = true;
    this.output.textContent = '';
    this.dataState.textContent = '';
    this.time.textContent = '';
    this.setState('loading', '正在刷新可用模型…');
    try {
      const data = await request.request(`${this.api}/api/ai/models`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
      });
      if (!request.active) return;
      this.setModels(data.models, data.models.includes(selected) ? selected : data.default_model);
      this.modelsReady = true;
      this.remember();
      this.setState('idle', '模型已就绪，切换品种或模型后自动解读。');
      this.scheduleAnalysis(true);
    } catch (error) {
      if (request.active) {
        this.setState('error', '模型列表暂不可用');
        this.output.textContent = `${error.message}。点击“刷新模型”重试。`;
      }
    } finally {
      if (this.connection === request) {
        this.refreshButton.disabled = false;
        this.model.disabled = !this.modelsReady;
      }
    }
  }

  setTarget(instrument, period, flowMode = 'realtime') {
    const changed = this.target?.symbol !== instrument.code || this.target?.period !== period;
    this.target = {symbol: instrument.code, period};
    const container = document.querySelector(instrument.model === 'nav' ? '#fund-view .right-panel' : '#stock-view .right-panel');
    container.prepend(this.root);
    this.context.textContent = `${instrument.name || instrument.code} · ${period === 'week' ? '周线' : '日线'}`;
    if (!changed) return;
    this.cancelAnalysis();
    this.flowEnabled = !!instrument.fund_flow;
    this.needsFlow = this.flowEnabled && flowMode === 'realtime';
    this.flowSnapshot = null;
    this.flowHealth = '';
    this.output.textContent = '';
    this.dataState.textContent = '';
    this.time.textContent = '';
    if (this.modelsReady) this.scheduleAnalysis();
  }

  setFlowMode(mode) {
    const needsFlow = this.flowEnabled && mode === 'realtime';
    if (needsFlow === this.needsFlow) return;
    this.cancelAnalysis();
    this.needsFlow = needsFlow;
    this.flowSnapshot = null;
    this.flowHealth = '';
    this.scheduleAnalysis(true);
  }

  setFlowSnapshot(symbol, data) {
    if (this.target?.symbol !== symbol || !this.needsFlow) return;
    const status = data.stale ? 'stale' : data.status || 'error';
    const health = JSON.stringify([status, data.summary_available, data.minute_status, data.source, data.data_date]);
    const changed = this.flowHealth && this.flowHealth !== health;
    this.flowSnapshot = {id: data.snapshot_id || null, status};
    this.flowHealth = health;
    if (!this.lastKey || changed) this.scheduleAnalysis(!!changed);
  }

  scheduleAnalysis(force = false) {
    if (!this.target || !this.modelsReady || !this.model.value) return;
    if (this.needsFlow && !this.flowSnapshot) {
      this.setState('loading', '正在等待页面资金流状态…');
      return;
    }
    const key = `${this.target.symbol}:${this.target.period}:${this.model.value}`;
    if (!force && key === this.lastKey) return;
    this.cancelAnalysis();
    this.output.textContent = '';
    this.dataState.textContent = '';
    this.time.textContent = '';
    this.setState('idle', '准备生成当前品种的解读…');
    this.timer = setTimeout(() => { this.timer = null; this.analyze(key); }, 350);
  }

  async analyze(key) {
    const request = this.request = new StockLoadSession(75000);
    this.lastKey = key;
    this.setState('analyzing', '正在结合最新行情生成解读…');
    try {
      const data = await request.request(`${this.api}/api/ai/analyze`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model: this.model.value, ...this.target,
          ...(this.needsFlow ? {flow_snapshot: this.flowSnapshot} : {})}),
      });
      if (!request.active) return;
      this.output.innerHTML = renderAiContent(data.text);
      if (data.fund_flow) {
        const flow = data.fund_flow.realtime;
        this.dataState.textContent = [flow.availability_note || flow.message,
          data.fund_flow.daily_history?.availability_note,
          flow.as_of ? `资金流截至 ${flow.as_of} · ${flow.source}` : ''].filter(Boolean).join('；');
      }
      this.time.textContent = new Date(data.analyzed_at).toLocaleTimeString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false});
      this.setState('ready', '解读已更新');
    } catch (error) {
      if (request.active) {
        this.lastKey = '';
        this.setState('error', '本次解读未完成');
        this.output.textContent = `${error.message}。可切换模型或点击“刷新模型”重试。`;
      }
    } finally {
      if (this.request === request) this.request = null;
    }
  }
}

if (typeof module !== 'undefined') module.exports = {AIAnalysisPanel, renderAiContent};
