/**
 * Hash-based SPA Router for 品牌AI内容工厂
 * Lives inside creation.html — toggles between inline creation UI and loaded pages.
 */
(function () {
  const PAGE_NAMES = {
    creation:          '开始创作',
    assets:            '资产库',
    contents:          '已生成内容',
    brands:            '品牌管理',
    'social-media':    '社媒数据',
    'ad-data':         '投放数据',
    'private-channel': '私域渠道',
    'user-mgmt':       '内容智能'
  };

  const cache = {};
  const contentEl  = () => document.getElementById('content');
  const creationEl = () => document.getElementById('creationUI');
  const topbarEl   = () => document.getElementById('creationTopbar');
  const breadEl    = () => document.getElementById('spaBreadcrumb');
  const bcTextEl   = () => document.getElementById('breadcrumb');

  async function loadPage(pageId) {
    const resp = await fetch(`/pages/${pageId}.html?_=${Date.now()}`);
    if (!resp.ok) throw new Error(`Page not found: ${pageId}`);
    return resp.text();
  }

  function inject(html) {
    const el = contentEl();
    el.innerHTML = html;
    el.querySelectorAll('script').forEach(old => {
      const s = document.createElement('script');
      if (old.src) s.src = old.src;
      else s.textContent = old.textContent;
      old.parentNode.replaceChild(s, old);
    });
  }

  function syncSidebar(pageId) {
    document.querySelectorAll('.nav-item').forEach(item => {
      item.classList.toggle('active', item.dataset.page === pageId);
    });
  }

  function syncBreadcrumb(pageId) {
    const el = bcTextEl();
    if (!el) return;
    el.innerHTML = `
      <span class="breadcrumb-item" onclick="navigateTo('creation')" style="cursor:pointer;">开始创作</span>
      <span class="breadcrumb-sep">›</span>
      <span class="breadcrumb-item active">${PAGE_NAMES[pageId] || pageId}</span>`;
  }

  window.navigateTo = function (pageId) {
    if (!PAGE_NAMES[pageId]) { console.warn('Unknown page:', pageId); return; }
    if (location.hash !== '#' + pageId) {
      location.hash = pageId;
      return;
    }
    render(pageId);
  };

  function show(el, visible) { if (el) el.style.display = visible ? '' : 'none'; }

  async function render(pageId) {
    syncSidebar(pageId);

    if (pageId === 'creation') {
      show(creationEl(), true);
      show(topbarEl(), true);
      show(contentEl(), false);
      show(breadEl(), false);
    } else {
      show(creationEl(), false);
      show(topbarEl(), false);
      show(contentEl(), true);
      show(breadEl(), true);
      syncBreadcrumb(pageId);
      try {
        const html = await loadPage(pageId);
        inject(html);
      } catch (err) {
        contentEl().innerHTML = `<div class="page"><div class="page-header"><h1 class="page-title">页面加载失败</h1><p class="page-subtitle">${err.message}</p></div></div>`;
      }
    }
  }

  function pageFromHash() {
    const h = location.hash.replace('#', '');
    return PAGE_NAMES[h] ? h : 'creation';
  }

  window.addEventListener('hashchange', () => render(pageFromHash()));
  document.addEventListener('DOMContentLoaded', () => render(pageFromHash()));
})();
