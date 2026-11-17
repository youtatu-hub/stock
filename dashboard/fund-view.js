// 场外基金使用净值折线；与场内 K 线共用页面导航、请求会话和指标计算。
class FundView {
  constructor(root, {colors, escHtml, calcMACD, calcRSI}) {
    this.root = root;
    this.colors = colors;
    this.escHtml = escHtml;
    this.calcMACD = calcMACD;
    this.calcRSI = calcRSI;
    this.chart = echarts.init(root.querySelector('#fund-chart'));
    this.data = null;
    this.period = 'day';
    this.range = 90;
    this.indicator = 'macd';
    for (const setting of ['period', 'range', 'indicator']) {
      root.querySelectorAll(`[data-fund-${setting}]`).forEach(button => {
        button.onclick = () => {
          const value = button.dataset[`fund${setting[0].toUpperCase()}${setting.slice(1)}`];
          this[setting] = setting === 'range' ? Number(value) : value;
          root.querySelectorAll(`[data-fund-${setting}]`).forEach(item => item.classList.toggle('active', item === button));
          this.renderChart();
        };
      });
    }
    this.resizeObserver = new ResizeObserver(() => { if (!root.hidden) this.chart.resize(); });
    this.resizeObserver.observe(root);
  }

  node(id) { return this.root.querySelector(`#fund-${id}`); }

  reset(sourceUrl) {
    this.data = null;
    this.chart.clear();
    this.chart.resize();
    this.chart.showLoading({text: '正在获取已披露净值…', color: '#ff9800', textColor: '#aaa', maskColor: '#000'});
    this.node('status').textContent = '正在读取基金历史净值…';
    this.node('summary').textContent = '分析加载中…';
    this.node('metrics').textContent = '';
    this.node('source-link').href = sourceUrl;
  }

  showError(message) {
    this.data = null;
    this.chart.hideLoading();
    this.chart.clear();
    this.node('status').textContent = `净值获取失败：${message}。可点击“刷新净值”重试。`;
    this.node('summary').textContent = '暂无可用分析';
    this.node('metrics').textContent = '';
  }

  render(data) {
    this.data = data;
    this.chart.hideLoading();
    const {analysis, history, latest} = data;
    const directionClass = analysis.direction === '上升' ? 'up' : analysis.direction === '下降' ? 'down' : 'flat';
    const descriptions = {
      上升: '复权净值高于 MA20，且 MA20 高于 MA60，均线呈多头排列。',
      下降: '复权净值低于 MA20，且 MA20 低于 MA60，均线呈空头排列。',
      震荡: '净值与中长期均线尚未形成一致方向。',
      数据不足: '不足 60 个连续有效净值日，暂不判断中长期趋势。',
    };
    this.node('summary').innerHTML = `<div class="fund-direction ${directionClass}">${this.escHtml(analysis.direction)}</div>
      ${descriptions[analysis.direction]}<br>以上按日净值计算，仅作趋势参考。`;
    const metrics = [
      ['近1周', analysis.returns.week, true], ['近1月', analysis.returns.month, true],
      ['近3月', analysis.returns.quarter, true], ['分析区间最大回撤', analysis.max_drawdown_pct, true],
      ['日均线 MA20', analysis.ma20, false], ['日均线 MA60', analysis.ma60, false],
    ];
    this.node('metrics').innerHTML = metrics.map(([label, value, percent]) => {
      const cls = percent && value != null ? value > 0 ? 'up' : value < 0 ? 'down' : 'flat' : '';
      const text = value == null ? '--' : percent ? `${value > 0 ? '+' : ''}${value.toFixed(2)}%` : value.toFixed(4);
      return `<div><dt>${label}</dt><dd class="${cls}">${text}</dd></div>`;
    }).join('');
    this.node('status').textContent = `${data.source} · 最新净值日 ${latest.date} · 已加载 ${history.length} 个净值日（${history[0].date} 至 ${latest.date}）。`
      + `分析区间 ${analysis.start_date} 至 ${analysis.end_date}；阶段涨跌幅以最新净值日为截止日。`
      + (analysis.has_growth_gap ? ' 部分历史日涨跌幅缺失，复权走势与分析仅覆盖连续有效区间。' : '')
      + (analysis.adjustment_days ? ` ${analysis.adjustment_days} 个净值跳变日按公布涨跌幅调整，精度受来源限制。` : '');
    this.renderChart();
  }

  chartPoints() {
    const points = this.data.history.filter(point => point.adjusted_nav != null);
    if (this.period === 'day') return points;
    const weeks = new Map();
    for (const point of points) {
      const monday = new Date(`${point.date}T00:00:00Z`);
      monday.setUTCDate(monday.getUTCDate() - (monday.getUTCDay() + 6) % 7);
      weeks.set(monday.toISOString().slice(0, 10), point);
    }
    return [...weeks.values()];
  }

  renderChart() {
    if (!this.data) return;
    const C = this.colors;
    const points = this.chartPoints();
    const dates = points.map(point => point.date);
    const values = points.map(point => point.adjusted_nav);
    const series = [{name: '复权净值', type: 'line', data: values, showSymbol: false,
      itemStyle: {color: C.text}, lineStyle: {color: C.text, width: 2}}];
    for (const period of [5, 20, 60]) {
      series.push({name: `日MA${period}`, type: 'line', data: points.map(point => point[`ma${period}`]), showSymbol: false,
        itemStyle: {color: C[`ma${period}`]}, lineStyle: {color: C[`ma${period}`], width: 1}});
    }
    if (this.indicator === 'macd') {
      const valuesMACD = this.calcMACD(values);
      for (const [key, label, color] of [['dif', 'DIF', C.ma5], ['dea', 'DEA', C.ma10]]) {
        series.push({name: label, type: 'line', xAxisIndex: 1, yAxisIndex: 1, showSymbol: false,
          data: valuesMACD[key], itemStyle: {color}, lineStyle: {color, width: 1}});
      }
      series.push({name: 'MACD', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
        data: valuesMACD.macd.map(value => ({value, itemStyle: {color: value >= 0 ? C.up : C.down}}))});
    } else {
      series.push({name: 'RSI14', type: 'line', xAxisIndex: 1, yAxisIndex: 1, showSymbol: false,
        data: this.calcRSI(values, 14), itemStyle: {color: C.ma10}, lineStyle: {color: C.ma10, width: 1}});
    }
    const cutoff = new Date(`${dates[dates.length - 1]}T00:00:00Z`);
    cutoff.setUTCDate(cutoff.getUTCDate() - this.range);
    const startIndex = this.range ? points.findIndex(point => point.date >= cutoff.toISOString().slice(0, 10)) : 0;
    const start = points.length > 1 ? Math.max(0, startIndex) / (points.length - 1) * 100 : 0;
    this.chart.setOption({
      animation: false, backgroundColor: C.bg,
      legend: {top: 10, textStyle: {color: C.text}, itemWidth: 14, itemHeight: 8},
      grid: [{left: 65, right: 35, top: 50, height: '52%'}, {left: 65, right: 35, top: '70%', height: '18%'}],
      xAxis: [0, 1].map(index => ({type: 'category', data: dates, gridIndex: index,
        axisLabel: {color: C.textDim, show: index === 1}, axisLine: {lineStyle: {color: C.axis}}})),
      yAxis: [0, 1].map(index => ({type: 'value', scale: true, gridIndex: index,
        min: index === 1 && this.indicator === 'rsi' ? 0 : null,
        max: index === 1 && this.indicator === 'rsi' ? 100 : null,
        axisLabel: {color: C.textDim}, splitLine: {lineStyle: {color: C.grid}}})),
      axisPointer: {link: [{xAxisIndex: 'all'}]},
      tooltip: {trigger: 'axis', backgroundColor: '#141414', borderColor: C.axis, textStyle: {color: C.text},
        formatter: params => {
          const point = points[params[0].dataIndex];
          return `${point.date}<br>单位净值：${point.nav.toFixed(4)}<br>` + params.map(item => {
            const value = typeof item.data === 'object' && item.data != null ? item.data.value : item.data;
            return `${item.marker}${item.seriesName}：${value == null ? '--' : Number(value).toFixed(4)}`;
          }).join('<br>');
        }},
      dataZoom: [{type: 'inside', xAxisIndex: [0, 1], start, end: 100},
        {type: 'slider', xAxisIndex: [0, 1], start, end: 100, bottom: 5, height: 18,
          borderColor: C.axis, textStyle: {color: C.textDim}}],
      series,
    }, true);
  }
}

if (typeof module !== 'undefined') module.exports = {FundView};
