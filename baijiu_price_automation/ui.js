const $ = id => document.getElementById(id);
const modeCards = [...document.querySelectorAll('.mode-card')];
const num = value => value == null ? '—' : Number(value).toLocaleString('zh-CN');
let polling = false, wasRunning = false, latestRun = null, displayedKey = '', config = null;
let notifiedJob = null;

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(svg.namespaceURI, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}
async function api(path, body) {
  const res = await fetch(path, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : undefined);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || '请求失败');
  return data;
}
const hints = {
  rpa:'请先登录 Windows 微信并打开搜一搜。更新期间不要关闭搜一搜窗口，请保持桌面解锁，暂停鼠标和键盘操作。',
  rpa_links:'粘贴微信原文链接，解析 HTML 表格与正文图片，运行本地 OCR 并更新数据库。无需微信读书授权。',
  history:'需要本机 WeRSS 服务及微信读书授权。每账号只返回最新一篇；历史补抓依赖公开合集，无法替代 RPA 历史采集。',
  local:'读取已导入的文章，解析行情并生成新的 Excel 数据库副本。',
  auto:'公开搜索六个公众号并核验日期。遇到微信验证或无法取得价格时记录缺失。',
  dry:'解析已导入文章，查看预计写入位置，只生成复核文件。',
  sample:'使用两篇模拟文章验证流程，固定日期 2026-06-04。输出不可作为真实行情。'
};
function updateMode() {
  const mode = $('mode').value;
  modeCards.forEach(card => card.setAttribute('aria-pressed', String(card.dataset.mode === mode)));
  $('modehelp').textContent = hints[mode];
  if (!$('run').disabled) $('run-label').textContent = mode === 'sample' ? '运行样例验证' : mode === 'dry' ? '开始试运行' : '开始更新数据库';
  $('seedlabel').textContent = mode === 'history' ? '原文 / 行情合集链接' : '微信原文链接';
  $('seed-required').textContent = mode === 'rpa_links' ? '必填' : '可选';
  $('seedurls').required = mode === 'rpa_links';
  $('seedurls').placeholder = mode === 'history' ? '每行一个微信原文或公开合集链接' : '每行一个 mp.weixin.qq.com 原文链接';
  $('seedhelp').textContent = mode === 'history' ? '公开合集可补抓历史文章，已发现的合集会留存供下次使用。' : '可补充指定日期的行情原文，核验公众号与原始日期后入库。';
  if (['local', 'dry', 'sample'].includes(mode)) $('import-details').open = true;
}
modeCards.forEach(card => card.addEventListener('click', () => { $('mode').value = card.dataset.mode; updateMode(); }));
$('mode').addEventListener('change', updateMode);

function selectTab(name) {
  ['quotes', 'review'].forEach(tab => {
    const active = tab === name;
    $(tab + '-tab').setAttribute('aria-selected', String(active));
    $(tab + '-tab').tabIndex = active ? 0 : -1;
    $(tab + '-pane').hidden = !active;
  });
}
['quotes', 'review'].forEach(tab => {
  $(tab + '-tab').addEventListener('click', () => selectTab(tab));
  $(tab + '-tab').addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 'quotes' : event.key === 'End' ? 'review' : tab === 'quotes' ? 'review' : 'quotes';
    selectTab(next); $(next + '-tab').focus();
  });
});

function table(id, title, heads, rows) {
  const root = $(id);
  root.replaceChildren();
  if (!rows.length) {
    if (id === 'quotes' || id === 'rpaReview') {
      const p = document.createElement('p'); p.className = 'help'; p.textContent = id === 'quotes' ? '本次尚无确认报价。' : '没有表格或图片识别复核记录。'; root.append(p);
    }
    return;
  }
  const h = document.createElement('h3'); h.textContent = title; root.append(h);
  function build(subset) {
    const wrap = document.createElement('div'); wrap.className = 'table-wrap';
    const t = document.createElement('table'), thead = document.createElement('thead'), tr = document.createElement('tr');
    heads.forEach(text => { const th = document.createElement('th'); th.textContent = text; th.scope = 'col'; tr.append(th); });
    thead.append(tr); t.append(thead);
    const tbody = document.createElement('tbody');
    subset.forEach(row => {
      const tr = document.createElement('tr');
      row.forEach((text, i) => { const td = document.createElement('td'); td.textContent = text ?? ''; if (heads[i] === '价格 / 元') td.className = 'price'; if (heads[i] === '写入位置') td.className = 'location'; tr.append(td); });
      tbody.append(tr);
    });
    t.append(tbody); wrap.append(t); return wrap;
  }
  root.append(build(rows.slice(0, 8)));
  if (rows.length > 8) {
    const details = document.createElement('details'); details.className = 'more-rows';
    const summary = document.createElement('summary'); summary.textContent = '展开其余 ' + (rows.length - 8) + ' 条预览记录';
    details.append(summary, build(rows.slice(8))); root.append(details);
  }
}
function fileLabel(name) {
  if (name.endsWith('.xlsx')) return '下载 Excel 数据库';
  if (name.startsWith('matches_')) return '报价匹配记录';
  if (name.startsWith('missing_review_')) return '未取得报价复核';
  return '核验与证据报告';
}
function updateKpis(r) {
  // Only a real published database updates the overview, never a sample/dry run.
  if (!r || r.sample || !r.excel) return;
  $('quote-count').textContent = num(r.prices);
  $('cell-count').textContent = num(r.cells);
  $('quote-period').textContent = r.start + ' — ' + r.end;
}
let quoteRows = [];
function renderQuotes() {
  const brand = $('brand-filter').value;
  const rows = quoteRows.filter(x => brand === 'all' || x.sheet === brand);
  table('quotes', '已确认报价 · ' + (brand === 'all' ? '全部品牌' : brand), ['日期', '公众号', '酒款', '价格 / 元', '写入位置'], rows.map(x => [x.date?.slice(5), x.account, x.product, num(x.price), (x.sheet || '') + '!' + (x.cell || '')]));
}
$('brand-filter').addEventListener('change', renderQuotes);
function renderResult(r, live = false) {
  $('empty-result').hidden = !!r;
  $('result-content').hidden = !r;
  $('result-data').hidden = !r;
  $('results-title').textContent = live ? '本次运行结果' : '最新数据库概览';
  $('results-subtitle').textContent = r?.sample ? '模拟结果，仅用于验证流程' : '读取真实任务记录与报价证据';
  if (!r) return;
  $('result-caption').textContent = r.verified ? '最近核验结果' : live ? '本次任务' : '最近成功任务';
  $('result-period').textContent = r.start + ' — ' + r.end;
  $('stats').hidden = false; $('stats').replaceChildren();
  [['确认报价', r.prices], ['写入位置', r.cells], ['未取得报价', r.missing]].forEach(([label, value]) => {
    const d = document.createElement('div'), n = document.createElement('strong'); n.textContent = num(value); d.append(n, label); $('stats').append(d);
  });
  const articleCount = r.articles ?? (r.rpa_articles || []).filter(a => a.status === 'processed').length;
  $('result-note').replaceChildren(icon(r.verified ? 'check' : 'file'), document.createTextNode(r.verified ? '报价证据、历史数据与均价已核验。' + (articleCount ? '来自 ' + articleCount + ' 篇行情文章。' : '') : r.sample ? '模拟报价仅用于验证，不计入数据库概览。' : '保留原始证据，按 Excel 模板匹配报价。'));
  $('alert').textContent = r.sample ? '当前结果来自模拟文章，不可作为真实行情使用。' : '';
  $('quotes-count').textContent = num(r.prices);
  $('review-count').textContent = num(r.review_count ?? (r.rpa_review || []).length);
  quoteRows = r.preview || []; renderQuotes();
  $('brands').replaceChildren();
  (r.brand_quotes || []).forEach(x => { const chip = document.createElement('span'), b = document.createElement('b'); chip.className = 'source-chip'; b.textContent = num(x.quotes); chip.append(x.brand, b); $('brands').append(chip); });
  table('rpaReview', '表格与图片识别复核', ['日期', '公众号', '酒款', '原因'], (r.rpa_review || []).map(x => [x.date, x.account, x.product, x.reason]));
  table('gaps', '未取得报价 · 含正常未公布项', ['日期', '公众号', '酒款', '原因'], (r.gaps || []).map(x => [x.date, x.account, x.product, x.reason]));
  table('coverage', '逐日公众号行情检查', ['日期', '公众号', '状态'], (r.article_coverage || []).map(x => [x.date, x.account, x.status]));
  $('sources').replaceChildren();
  (r.account_quotes || []).forEach(x => { const chip = document.createElement('span'), b = document.createElement('b'); chip.className = 'source-chip'; b.textContent = num(x.quotes); chip.append(x.account, b); $('sources').append(chip); });
  $('downloads').replaceChildren();
  [...new Set([r.excel, ...(r.files || [])].filter(Boolean))].forEach(name => {
    const a = document.createElement('a'); a.href = '/download/' + encodeURIComponent(name); a.title = name; a.append(icon('download'), fileLabel(name)); $('downloads').append(a);
  });
}
let activeJobId = null;
function show(s) {
  activeJobId = s.job_id;
  const pausing = s.pause_requested && !s.paused;
  $('pause-job').hidden = !(s.running && s.pausable && !s.pause_requested);
  $('resume-job').hidden = !(s.running && s.pause_requested);
  $('resume-job').textContent = s.paused ? '继续采集' : '取消暂停';
  const failed = s.message.startsWith('运行失败');
  $('job-progress').hidden = !(s.running || s.job_id);
  $('job-progress').setAttribute('aria-busy', String(s.running));
  $('job-progress').classList.toggle('failed', failed);
  $('progress-title').textContent = s.paused ? '任务已暂停' : pausing ? '正在暂停' : s.running ? '正在更新数据库' : failed ? '更新失败，请重试' : s.result?.collection_incomplete ? '部分更新完成' : '任务结束';
  $('progress-bar').value = s.progress || 0;
  $('progress-value').textContent = (s.progress || 0) + '%';
  $('progress-message').textContent = s.paused ? '已保留当前进度，点击继续采集即可接着处理。保持微信列表打开，继续前不要改变页面。' : pausing ? '正在完成当前文章并返回列表，随后暂停。' : s.message;
  $('progress-reminder').hidden = !(s.running && !s.paused && s.mode === 'rpa');
  $('state').textContent = s.paused ? '已暂停' : pausing ? '正在暂停' : s.running ? '正在运行' : s.message.startsWith('运行失败') ? '运行失败' : s.result ? '任务完成' : latestRun?.verified ? '已核验数据库' : latestRun ? '数据库已就绪' : '等待操作';
  $('state').parentElement.title = s.message;
  $('task-pill').classList.toggle('busy', s.running);
  $('task-pill').classList.toggle('failed', s.message.startsWith('运行失败'));
  $('run').disabled = s.running; $('upload').disabled = s.running;
  modeCards.forEach(card => card.disabled = s.running);
  // Lock the task configuration while the posted job is running.
  ['workbook','start','end','mode','seedurls','articles','database-open','database-file'].forEach(id => $(id).disabled = s.running);
  if ($('log').textContent !== (s.log || '尚未运行')) $('log').textContent = s.log || '尚未运行';
  if (s.running) $('run-label').textContent = '正在更新数据库'; else updateMode();
  const r = s.result || latestRun;
  const key = JSON.stringify([!!s.result, r?.excel, r?.mode, r?.prices, r?.cells, r?.start, r?.end]);
  if (key !== displayedKey) { renderResult(r, !!s.result); displayedKey = key; }
  if (s.running) $('results-subtitle').textContent = s.message;
  if (s.message.startsWith('运行失败')) $('alert').textContent = s.message;
  if (s.result) updateKpis(s.result);
}
async function refreshDatabases(initial = false) {
  const c = await api('/api/config'); config = c;
  const old = $('workbook').value;
  $('workbook').replaceChildren();
  c.workbook_options.forEach(item => { const o = document.createElement('option'); o.value = item.value; o.textContent = item.label; $('workbook').append(o); });
  if (polling && c.workbooks.includes(old)) $('workbook').value = old;
  else if (c.default_workbook) $('workbook').value = c.default_workbook;
  $('versions').replaceChildren();
  c.database_versions.forEach(item => {
    const box = document.createElement('div'); box.className = 'version-card' + (item.role === 'latest' && item.available ? ' latest' : '') + (!item.available ? ' unavailable' : '');
    const top = document.createElement('div'), badge = document.createElement('span'); top.className = 'version-top';
    badge.textContent = item.role === 'latest' ? '默认接续' : item.role === 'previous' ? '更新前版本' : '历史检查点'; top.append(icon(item.available ? 'db' : 'clock'), badge);
    const title = document.createElement('strong'), time = document.createElement('span'); title.textContent = item.label; time.className = 'version-time';
    time.textContent = item.available ? item.created_at.slice(0,16).replace('T',' ') : '尚无快照';
    box.append(top, title, time);
    if (item.available) { const a = document.createElement('a'); a.href = '/download/' + encodeURIComponent(item.name); a.append('下载数据库', icon('arrow')); box.append(a); }
    else { const p = document.createElement('span'); p.className = 'placeholder'; p.textContent = '随历史积累自动保留'; box.append(p); }
    $('versions').append(box);
  });
  const latest = c.database_versions.find(x => x.role === 'latest' && x.available);
  $('last-updated').textContent = latest ? latest.created_at.slice(0,16).replace('T','  ') : '暂无数据库';
  if (initial) { $('start').value = c.start; $('end').value = c.end; $('inputs').textContent = '文章目录已有 ' + c.inputs + ' 个文件'; }
}
async function loadOverview() {
  const overview = await api('/api/overview'); latestRun = overview.latest_run;
  $('account-count').textContent = String(overview.accounts.length).padStart(2,'0');
  $('product-count').textContent = num(overview.products);
  updateKpis(latestRun);
}
async function poll() {
  try {
    const s = await api('/api/status'); show(s);
    $('service').replaceChildren(icon('check'), '本机服务已连接');
    if ($('alert').textContent.startsWith('服务连接中断')) $('alert').textContent = '';
    if (s.running) { wasRunning = true; polling = true; setTimeout(poll, 1000); }
    else {
      polling = false; await refreshDatabases();
      if (wasRunning) {
        wasRunning = false;
        await loadOverview();
        if (!s.result) { displayedKey = ''; show(s); }
        notifyCompletion(s);
      }
    }
  } catch (e) {
    $('alert').textContent = '服务连接中断，正在重新连接。请保留启动窗口。';
    $('service').textContent = '服务连接中断';
    if (wasRunning || polling) setTimeout(poll, 2000);
  }
}
function notifyCompletion(s) {
  if (!s.job_id || notifiedJob === s.job_id) return;
  notifiedJob = s.job_id;
  const r = s.result;
  const success = r?.excel && r.prices > 0 && !s.message.startsWith('运行失败');
  const dry = r?.mode === 'dry' && r.prices > 0;
  $('completion-title').textContent = dry ? '试运行完成，未写入数据库' : success ? (r.sample ? '样例验证完成（模拟价格）' : r.collection_incomplete ? '部分更新完成，可下载数据库' : '更新成功，可下载数据库') : '更新失败，请重试';
  $('completion-message').textContent = dry ? '匹配结果已生成，可在报价和复核记录中查看。' : success
    ? `本次确认 ${num(r.prices)} 条报价，写入 ${num(r.cells)} 个位置。` + (r.collection_incomplete ? '部分公众号采集失败，已保留取得的数据，请补录缺少的文章或重试。' : '请检查复核记录；公众号未公布的品类保持空白。')
    : s.message + '。请检查运行日志，确认微信已登录、搜一搜保持打开后重试。';
  $('completion-download').hidden = !success;
  if (success) $('completion-download').href = '/download/' + encodeURIComponent(r.excel);
  if (!$('completion').open) $('completion').showModal();
}
for (const [id, route] of [['pause-job', '/api/pause'], ['resume-job', '/api/resume']]) {
  $(id).addEventListener('click', async () => {
    $(id).disabled = true;
    try { await api(route, {job_id:activeJobId}); show(await api('/api/status')); }
    catch (e) { $('alert').textContent = e.message; }
    finally { $(id).disabled = false; }
  });
}
$('completion-close').addEventListener('click', () => $('completion').close());
$('job').addEventListener('submit', async event => {
  event.preventDefault(); $('alert').textContent = '';
  try {
    const days = (Date.parse($('end').value) - Date.parse($('start').value)) / 86400000;
    if (!Number.isFinite(days) || days < 0 || days > 31) throw new Error('请选择不超过 32 天的有效日期区间');
    $('run').disabled = true;
    await api('/api/run', {mode:$('mode').value, start:$('start').value, end:$('end').value, workbook:$('workbook').value, seed_urls:$('seedurls').value});
    wasRunning = true; polling = true; await poll();
  } catch (e) { $('run').disabled = false; updateMode(); $('alert').textContent = e.message; }
});
$('upload').addEventListener('click', async () => {
  try {
    const files = [];
    for (const file of $('articles').files) {
      const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file); });
      files.push({name:file.name, data});
    }
    if (!files.length) throw new Error('请先选择文章文件');
    $('upload').disabled = true;
    const res = await api('/api/upload', {files}); $('inputs').textContent = '已导入：' + res.saved.join('、'); $('articles').value = ''; $('alert').textContent = '';
  } catch (e) { $('alert').textContent = e.message; }
  finally { $('upload').disabled = false; }
});
$('guide-open').addEventListener('click', () => $('guide').showModal());
$('database-open').addEventListener('click', () => $('database-file').click());
$('database-file').addEventListener('change', async () => {
  const file = $('database-file').files[0]; if (!file) return;
  const feedback = $('database-feedback'); feedback.classList.remove('failed');
  $('database-open').disabled = true; $('run').disabled = true;
  try {
    if (!file.name.toLowerCase().endsWith('.xlsx') || file.size > 50 * 1024 * 1024) throw new Error('请选择不超过 50MB 的 XLSX 数据库');
    feedback.textContent = '正在上传并检查数据库模板…';
    const data = await new Promise((resolve,reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file); });
    const result = await api('/api/database', {name:file.name,data});
    await refreshDatabases(); $('workbook').value = result.value;
    feedback.textContent = '已导入 ' + result.original_name + '，已有报价截至 ' + (result.latest_price_date || '未识别日期') + '。后续更新接续此副本。';
    $('alert').textContent = '';
  } catch (e) { feedback.classList.add('failed'); feedback.textContent = '导入失败：' + e.message; }
  finally { $('database-open').disabled = false; $('run').disabled = false; updateMode(); $('database-file').value = ''; }
});
$('guide-close').addEventListener('click', () => $('guide').close());
document.querySelectorAll('.nav a').forEach(a => a.addEventListener('click', () => { document.querySelectorAll('.nav a').forEach(x => x.classList.toggle('active', x === a)); }));
(async () => {
  try { await refreshDatabases(true); await loadOverview(); updateMode(); await poll(); }
  catch (e) { renderResult(null); $('alert').textContent = '连接失败：' + e.message; $('service').textContent = '服务未连接'; }
})();
