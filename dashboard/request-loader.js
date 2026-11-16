// 一次选股会话：每个阶段独立完成，切换股票时取消旧请求。
class StockLoadSession {
  constructor(timeoutMs = 10000) {
    this.timeoutMs = timeoutMs;
    this.controller = new AbortController();
  }

  get active() {
    return !this.controller.signal.aborted;
  }

  cancel() {
    this.controller.abort();
  }

  async request(url) {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    this.controller.signal.addEventListener('abort', cancel, { once: true });
    if (!this.active) cancel();
    const timer = setTimeout(cancel, this.timeoutMs);
    try {
      const response = await fetch(url, { signal: controller.signal });
      const data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    } catch (error) {
      if (controller.signal.aborted && this.active) throw new Error('请求超时，请稍后重试');
      throw error;
    } finally {
      clearTimeout(timer);
      this.controller.signal.removeEventListener('abort', cancel);
    }
  }

  async run(stage, url, onSuccess, onError) {
    const started = performance.now();
    try {
      const data = await this.request(url);
      if (this.active) onSuccess(data);
    } catch (error) {
      if (this.active) {
        console.warn(`[${stage}]`, error);
        onError(error);
      }
    } finally {
      console.info(`[load_stage] stage=${stage} elapsed_ms=${(performance.now() - started).toFixed(1)} active=${this.active}`);
    }
  }
}

if (typeof module !== 'undefined') module.exports = { StockLoadSession };
