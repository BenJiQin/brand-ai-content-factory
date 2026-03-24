// api/client.js — 统一 API 客户端

const API_BASE = '';  // same origin

export const api = {
  // ---- 品牌资产 ----
  async getBrands() {
    const r = await fetch(`${API_BASE}/api/brands`);
    return r.json();
  },
  async getSkuImages(brand, sku) {
    const r = await fetch(`${API_BASE}/api/brands/${encodeURIComponent(brand)}/${encodeURIComponent(sku)}/images`);
    return r.json();
  },

  // ---- 任务 ----
  async getTasks(limit = 30) {
    const r = await fetch(`${API_BASE}/api/tasks?limit=${limit}`);
    return r.json();
  },
  async pollTask(taskId) {
    const r = await fetch(`${API_BASE}/api/task/${taskId}`);
    return r.json();
  },

  // ---- 视频生成 ----
  async text2video({ prompt, duration = 10, ratio = '9:16', model = '2.0', audio = true }) {
    const fd = new FormData();
    fd.append('prompt', prompt);
    fd.append('duration', duration);
    fd.append('ratio', ratio);
    fd.append('model', model);
    fd.append('audio', audio);
    const r = await fetch(`${API_BASE}/api/generate/text2video`, { method: 'POST', body: fd });
    return r.json();
  },
  async image2video({ prompt = '', image_url, duration = 10, ratio = '9:16', model = '2.0', audio = true }) {
    const fd = new FormData();
    fd.append('prompt', prompt);
    fd.append('image_url', image_url);
    fd.append('duration', duration);
    fd.append('ratio', ratio);
    fd.append('model', model);
    fd.append('audio', audio);
    const r = await fetch(`${API_BASE}/api/generate/image2video`, { method: 'POST', body: fd });
    return r.json();
  },
  async reference2video({ prompt, image_urls = [], duration = 10, ratio = '9:16', model = '2.0', audio = true }) {
    const fd = new FormData();
    fd.append('prompt', prompt);
    fd.append('image_urls', JSON.stringify(image_urls));
    fd.append('duration', duration);
    fd.append('ratio', ratio);
    fd.append('model', model);
    fd.append('audio', audio);
    const r = await fetch(`${API_BASE}/api/generate/reference`, { method: 'POST', body: fd });
    return r.json();
  },

  // ---- 文案生成（SSE流式）----
  generateCopy({ prompt, brand, sku, mode, onChunk, onDone, onError }) {
    const fd = new FormData();
    fd.append('prompt', prompt);
    fd.append('brand', brand || '');
    fd.append('sku', sku || '');
    fd.append('mode', mode || 'video_script');

    fetch(`${API_BASE}/api/generate/copy`, { method: 'POST', body: fd })
      .then(async res => {
        if (!res.ok) {
          const err = await res.text();
          onError?.(err);
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) { onDone?.(); break; }
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop();
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              const data = line.slice(6).trim();
              if (data === '[DONE]') { onDone?.(); return; }
              try { const obj = JSON.parse(data); onChunk?.(obj.text || ''); } catch {}
            }
          }
        }
      })
      .catch(e => onError?.(e.message));
  },

  // ---- 上传 ----
  async uploadFile(file) {
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch(`${API_BASE}/api/upload`, { method: 'POST', body: fd });
    return r.json();
  },

  // ---- 社媒数据 ----
  async getSocialPublish({ platform, brand } = {}) {
    const params = new URLSearchParams();
    if (platform) params.set('platform', platform);
    if (brand) params.set('brand', brand);
    const q = params.toString();
    const r = await fetch(`${API_BASE}/api/social-publish${q ? '?' + q : ''}`);
    return r.json();
  },
  async createSocialPublish(data) {
    const r = await fetch(`${API_BASE}/api/social-publish`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
    });
    return r.json();
  },
  async updateSocialPublish(id, data) {
    const r = await fetch(`${API_BASE}/api/social-publish/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
    });
    return r.json();
  },
  async deleteSocialPublish(id) {
    const r = await fetch(`${API_BASE}/api/social-publish/${id}`, { method: 'DELETE' });
    return r.json();
  },
  async importSocialCsv(file) {
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch(`${API_BASE}/api/social-publish/import-csv`, { method: 'POST', body: fd });
    return r.json();
  },
  async getContentTasks() {
    const r = await fetch(`${API_BASE}/api/content-tasks`);
    return r.json();
  },

  // ---- 投放数据 ----
  async getAdCampaigns({ platform, brand } = {}) {
    const params = new URLSearchParams();
    if (platform) params.set('platform', platform);
    if (brand) params.set('brand', brand);
    const q = params.toString();
    const r = await fetch(`${API_BASE}/api/ad-campaigns${q ? '?' + q : ''}`);
    return r.json();
  },
  async createAdCampaign(data) {
    const r = await fetch(`${API_BASE}/api/ad-campaigns`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
    });
    return r.json();
  },
  async updateAdCampaign(id, data) {
    const r = await fetch(`${API_BASE}/api/ad-campaigns/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
    });
    return r.json();
  },
  async deleteAdCampaign(id) {
    const r = await fetch(`${API_BASE}/api/ad-campaigns/${id}`, { method: 'DELETE' });
    return r.json();
  },
  async importAdCsv(file) {
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch(`${API_BASE}/api/ad-campaigns/import-csv`, { method: 'POST', body: fd });
    return r.json();
  },
};

// 轮询工具：定期检查任务状态
export function pollUntilDone(taskId, { onUpdate, onDone, onError, interval = 30000, maxAttempts = 20 }) {
  let attempts = 0;
  const timer = setInterval(async () => {
    attempts++;
    try {
      const res = await api.pollTask(taskId);
      onUpdate?.(res);
      const status = res?.data?.status || res?.status;
      if (status === 'succeeded' || status === 'failed' || status === 'error' || attempts >= maxAttempts) {
        clearInterval(timer);
        if (status === 'succeeded') onDone?.(res);
        else onError?.(res);
      }
    } catch (e) {
      clearInterval(timer);
      onError?.(e.message);
    }
  }, interval);
  return () => clearInterval(timer); // return cancel fn
}
