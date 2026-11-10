'use strict';
const $ = selector => document.querySelector(selector);
const token = $('meta[name="csrf-token"]').content;
let state = null, busy = false, timer, toastTimer, currentJobId = null, lastJobStatus = null;
let activePage = 'accounts', accountProvider = 'claude', codexState = null;
const accountApi = path => accountProvider === 'codex' ? '/api/codex/'+path : '/api/'+path;
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, body) {
  const response = await fetch(path, {method:body === undefined ? 'GET':'POST', credentials:'same-origin', headers:{'X-CSRF-Token':token, ...(body === undefined ? {}:{'Content-Type':'application/json'})}, ...(body === undefined ? {}:{body:JSON.stringify(body)})});
  const result = await response.json();
  if (response.status === 401) { location.reload(); throw new Error('请重新登录面板。'); }
  if (!response.ok) throw new Error(result.error || '请求失败，请重试。');
  return result;
}
$('#logout')?.addEventListener('click', async event => {
  event.currentTarget.disabled = true;
  try { await api('/api/auth/logout', {}); location.reload(); }
  catch(error) { toast(error.message, true); event.currentTarget.disabled = false; }
});
function toast(message, error = false) {
  clearTimeout(toastTimer); $('#toast').textContent = message; $('#toast').className = 'toast' + (error ? ' error':''); $('#toast').hidden = false;
  toastTimer = setTimeout(() => $('#toast').hidden = true, error ? 8000:5500);
}
async function action(button, work) {
  if (busy) return;
  busy = true; if (button) button.disabled = true;
  try { await work(); await load(); } catch(error) { toast(error.message, true); }
  finally { busy = false; if (button) button.disabled = false; }
}
function render() {
  const display = accountProvider === 'codex' ? codexState : state;
  if (!display) return;
  const active = display.accounts.find(a => a.id === display.activeId);
  $('#account-count').textContent = `${display.accounts.length} 个账号`;
  const markup = display.accounts.map(a => {
    const auth = a.auth || {}, isActive = a.id === display.activeId;
    return `<article class="account${isActive ? ' active':''}"><div class="avatar" aria-hidden="true">${escapeHTML(a.name.slice(0,1))}</div><div class="account-info"><div class="account-name">${escapeHTML(a.name)}${isActive ? '<span class="badge">当前</span>':''}${!auth.loggedIn ? '<span class="badge pending">待登录</span>':''}</div><div class="account-email">${escapeHTML(auth.email || '尚未登录')}${auth.subscriptionType ? ' · '+escapeHTML(auth.subscriptionType):''}</div><div class="account-source">${a.source === 'linked' ? '本机账号':'独立账号'} · ${a.historyShared ? '共享历史':'历史路径异常'}</div></div><div class="account-actions">${!auth.loggedIn && a.source === 'managed' ? `<button class="secondary" data-action="login" data-id="${a.id}">去登录</button>`:isActive ? '':`<button class="secondary" data-action="activate" data-id="${a.id}">切换</button>`}<button class="quiet" data-action="refresh" data-id="${a.id}" aria-label="检查 ${escapeHTML(a.name)} 的登录状态">检查状态</button>${auth.loggedIn && a.source === 'managed' && !isActive ? `<button class="quiet" data-action="login" data-id="${a.id}">重新登录</button>`:''}</div></article>`;
  }).join('') || '<div class="empty">添加第一个账号，用官方登录开始。</div>';
  // Avoid replacing focused buttons on each status poll.
  if ($('#accounts').dataset.markup !== markup) { $('#accounts').innerHTML = markup; $('#accounts').dataset.markup = markup; }
  $('#current-account').textContent = active?.name || '尚未选择';
  const processes = state.processes.length;
  $('#process-status').textContent = accountProvider === 'codex' ? '下次启动生效' : processes ? `${processes} 个 Claude 进程运行中`:'可切换';
  $('#process-status').className = 'process-status' + (processes ? ' busy':'');
  $('#entry-config').textContent = `CLAUDE_CONFIG_DIR=${state.entryPath}`;
  $('#launcher-path').textContent = state.integration.executablePath;
  $('#paths').innerHTML = [['固定账号入口',state.entryPath],['共享历史目录',state.sharedPath],['Claude CLI',state.claudePath]].map(([k,v])=>`<dt>${k}</dt><dd>${escapeHTML(v)}</dd>`).join('');
  renderJob(display.jobs[0]);
  const entry = $('#codex-entry');
  if(entry) { entry.hidden = accountProvider !== 'codex'; entry.textContent = accountProvider === 'codex' ? '终端启动入口：'+display.launcher+' · 切换后新启动的 Codex 生效' : ''; }
  $('#code-form').hidden = accountProvider === 'codex' || !display.jobs.some(j=>j.status === 'running');
}
function renderJob(job) {
  $('#login-panel').hidden = !job;
  if (!job) return;
  $('.login-instructions').hidden = job.status === 'completed';
  const account = (accountProvider === 'codex' ? codexState : state).accounts.find(a=>a.id === job.accountId);
  $('#login-heading').textContent = `登录 · ${account?.name || 'Claude 账号'}`;
  const labels = {running:'等待官方授权',completed:'登录成功',failed:'登录未完成',cancelled:'已取消'};
  $('#login-badge').textContent = labels[job.status] || job.status;
  $('#login-badge').className = 'badge' + (job.status === 'running' ? ' pending':job.status === 'failed' ? ' failed':job.status === 'cancelled' ? ' neutral':'');
  const descriptions = {
    running:['官方授权','打开授权页面，完成后返回。'],
    completed:['账号已保存','可以在上方切换账号。'],
    failed:['登录未完成','查看 CLI 输出，或重新登录。'],
    cancelled:['登录已取消','可从账号列表重新登录。']
  };
  const description = descriptions[job.status] || descriptions.running;
  $('#login-step-title').textContent = description[0];
  $('#login-step-description').textContent = description[1];
  const terminal = $('#terminal'), atEnd = terminal.scrollTop + terminal.clientHeight >= terminal.scrollHeight - 30;
  terminal.textContent = job.output || '正在启动官方 Claude 登录进程…';
  $('.cli-details').open = accountProvider === 'codex' && job.status === 'running';
  if (atEnd) terminal.scrollTop = terminal.scrollHeight;
  const linksMarkup = job.links.map((url,i)=>`<a class="login-link" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${i ? '打开备用授权链接':'打开官方授权页面'} ↗</a>`).join('');
  const links = $('#login-links');
  if (job.status === 'running') { if (links.dataset.markup !== linksMarkup) { links.innerHTML = linksMarkup; links.dataset.markup = linksMarkup; } }
  else { links.innerHTML = ''; links.dataset.markup = ''; }
  $('#login-wait').hidden = job.status !== 'running' || job.links.length > 0;
  $('#code-form').hidden = job.status !== 'running';
  $('#cancel-login').hidden = job.status !== 'running';
  $('#login-error').textContent = job.error || ''; $('#login-error').hidden = !job.error;
  if (job.id === currentJobId && lastJobStatus !== 'completed' && job.status === 'completed') toast('登录成功。');
  currentJobId = job.id; lastJobStatus = job.status;
}
async function load() {
  try { state = await api('/api/state'); if(accountProvider === 'codex') codexState = await api('/api/codex/state'); $('#connection-error').hidden = true; render(); }
  catch(error) { $('#connection-error').textContent = '无法连接本机服务：'+error.message; $('#connection-error').hidden = false; }
}
function toggleAdd(show) { $('#add-panel').hidden = !show; $('#add-toggle').setAttribute('aria-expanded', String(show)); if(show) $('#account-name').focus(); }
$('#add-toggle').addEventListener('click',()=>toggleAdd($('#add-panel').hidden));
$('#add-cancel').addEventListener('click',()=>toggleAdd(false));
$('#add-panel').addEventListener('submit',event=>{
  event.preventDefault(); const name = $('#account-name').value.trim(); if(!name) return;
  action(event.submitter,async()=>{
    const created = await api(accountApi('accounts'),{name});
    toggleAdd(false); $('#account-name').value='';
    try { await api(accountApi('login'),{id:created.id}); }
    catch(error) { toast('账号目录已创建：'+error.message,true); }
    await load(); $('#login-panel').scrollIntoView({behavior:'smooth',block:'start'});
  });
});
$('#accounts').addEventListener('click',event=>{
  const button = event.target.closest('button[data-action]'); if(!button) return;
  action(button,async()=>{
    const kind = button.dataset.action, result = await api(accountApi(kind),{id:button.dataset.id});
    if(kind === 'activate') toast('已切换，下次启动 '+(accountProvider === 'codex' ? 'Codex':'Claude')+' 生效。');
    if(kind === 'refresh') toast(result.loggedIn ? (accountProvider === 'codex' ? '已读取本地 Codex 登录凭据。':'已验证 Claude 订阅登录。'):result.error || '尚未登录。',!result.loggedIn);
    if(kind === 'login') { await load(); $('#login-panel').scrollIntoView({behavior:'smooth',block:'start'}); }
  });
});
$('#code-form').addEventListener('submit',event=>{
  event.preventDefault(); const value = $('#auth-code').value.trim(); if(!value) return;
  action(event.submitter,async()=>{ await api('/api/login/input',{id:currentJobId,input:value}); $('#auth-code').value=''; toast('已将授权码提交给官方 CLI。'); });
});
$('#send-enter').addEventListener('click',event=>action(event.currentTarget,()=>api('/api/login/input',{id:currentJobId,input:''})));
$('#cancel-login').addEventListener('click',event=>action(event.currentTarget,()=>api(accountApi('login/cancel'),{id:currentJobId})));
$('#copy-config').addEventListener('click',event=>action(event.currentTarget,async()=>{
  const text = `CLAUDE_CONFIG_DIR=${state.entryPath}`;
  try { await navigator.clipboard.writeText(text); toast('环境变量已复制。'); }
  catch { const selection=window.getSelection(), range=document.createRange(); range.selectNodeContents($('#entry-config')); selection.removeAllRanges(); selection.addRange(range); toast('已选中配置，请按 Ctrl+C 复制。'); }
}));
$('#copy-launcher').addEventListener('click',event=>action(event.currentTarget,async()=>{
  try { await navigator.clipboard.writeText(state.integration.executablePath); toast('启动器路径已复制。'); }
  catch { const selection=window.getSelection(),range=document.createRange();range.selectNodeContents($('#launcher-path'));selection.removeAllRanges();selection.addRange(range);toast('已选中启动器路径，请按 Ctrl+C 复制。'); }
}));
async function poll() { await load(); timer = setTimeout(poll, (accountProvider === 'codex' ? codexState : state)?.jobs.some(j=>j.status === 'running') ? 1000:4000); }
document.addEventListener('visibilitychange',()=>{ if(!document.hidden) { clearTimeout(timer); poll(); } });
poll();

let usageData = null, usageDays = 7, usageProvider = 'all', usageTimer, usageSequence = 0, quotaRefreshBusy = false;
let usagePeriod = null;
try { $('#billing-day').value = localStorage.getItem('usage-billing-day') || '1'; } catch {}
const number = value => new Intl.NumberFormat('zh-CN').format(value || 0);
const compact = value => new Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:1}).format(value || 0);
const money = value => value == null ? '—' : value > 0 && value < .01 ? '<$0.01' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(value);
const dateTime = value => value ? new Date(value*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'未知';
function feeCell(row) { return escapeHTML(money(row.estimatedUsd)) + (row.unpricedRequests ? `<small>${number(row.unpricedRequests)} 条未计价</small>`:''); }
function renderUsage() {
  if (!usageData) return;
  const data=usageData, index=data.index, summary=data.summary;
  const status=index.scanning ? `扫描中 ${number(index.completedFiles)} / ${number(index.totalFiles)}` : index.lastScannedAt ? `更新于 ${dateTime(index.lastScannedAt)}`:'读取中…';
  $('#usage-status').textContent=status;
  $('#quota-status').textContent=status;
  $('#usage-error').textContent=index.error || ''; $('#usage-error').hidden=!index.error;
  $('#quota-error').textContent=index.error || ''; $('#quota-error').hidden=!index.error;
  $('#period-ledger').innerHTML = [[1,'今天'],[7,'近 7 天'],[30,'近 30 天']].map(([days,label])=>{
    const row=data.overview[String(days)];
    return `<div class="period-cell"><span>${label}</span><strong>${row.requests ? escapeHTML(money(row.estimatedUsd)):'暂无记录'}</strong><small>${number(row.requests)} 条记录 · ${compact(row.totalTokens)} Token${row.unpricedRequests ? ' · '+number(row.unpricedRequests)+' 条未计价':''}</small></div>`;
  }).join('');
  $('#usage-summary').innerHTML = `<div class="selected-total"><strong>${compact(summary.totalTokens)}</strong><span>Token · ${number(summary.requests)} 条用量记录</span></div><dl>${[['输入',summary.inputTokens],['输出',summary.outputTokens],['缓存写入',summary.cacheWriteTokens],['缓存读取',summary.cacheReadTokens]].map(([label,value])=>`<div><dt>${label}</dt><dd title="${number(value)}">${compact(value)}</dd></div>`).join('')}<div><dt>缓存命中率</dt><dd>${summary.cacheHitRate == null ? '—':(summary.cacheHitRate*100).toFixed(1)+'%'}</dd></div></dl>`;
  $('#range-caption').textContent=data.range ? data.range.start+' 至 '+data.range.end+' · 本机日期':'';
  const cost=summary.costBreakdown;
  $('#usage-cost').innerHTML='<strong>'+escapeHTML(money(summary.estimatedUsd))+'</strong><span>API 等价费用 · '+number(summary.pricedRequests)+' / '+number(summary.requests)+' 条已计价'+(summary.unpricedRequests ? ' · '+number(summary.unpricedRequests)+' 条未计价':'')+'</span>'+(cost && summary.pricedRequests ? '<small>'+[['输入','input'],['输出','output'],['缓存写入','cacheWrite'],['缓存读取','cacheRead']].map(([label,key])=>label+' '+money(cost[key])).join(' · ')+'</small>':'');
  renderChart();
  $('#model-rows').innerHTML=data.models.map(row=>`<tr><th scope="row">${escapeHTML(row.model)}</th><td>${number(row.requests)}</td><td>${compact(row.inputTokens)}</td><td>${compact(row.outputTokens)}</td><td>${compact(row.cacheWriteTokens)}</td><td>${compact(row.cacheReadTokens)}</td><td>${feeCell(row)}</td></tr>`).join('') || '<tr><td colspan="7" class="no-usage">本机在所选区间没有可统计的模型回复。</td></tr>';
  $('#account-rows').innerHTML=data.accounts.map(row=>`<tr><th scope="row">${escapeHTML(row.name)}${row.historicalRequests || row.verifiedRequests ? `<small>历史会话 ${number(row.historicalRequests)} · 已确认 ${number(row.verifiedRequests)}</small>`:""}</th><td>${number(row.requests)}</td><td>${compact(row.totalTokens)}</td><td>${feeCell(row)}</td></tr>`).join('');
  $('#recent-rows').innerHTML=data.recent.map(row=>`<tr><td>${escapeHTML(dateTime(row.timestamp))}</td><th scope="row">${escapeHTML(row.model)}</th><td>${escapeHTML(row.account)}</td><td>${compact(row.totalTokens)}</td><td>${escapeHTML(money(row.estimatedUsd))}</td></tr>`).join('') || '<tr><td colspan="5" class="no-usage">暂无记录。</td></tr>';
  $('#attribution-note').textContent=data.attributionNote;
  $('#quota-refresh').textContent='读取缓存';
  $('#quota-rows').innerHTML=data.quotas.map(account=>`<div class="quota-account"><strong>${escapeHTML(account.name)}${account.provider==='claude' ? `<button type="button" class="quiet" data-quota-account="${escapeHTML(account.accountId)}" ${quotaRefreshBusy?'disabled':''} aria-label="手动刷新 ${escapeHTML(account.name)} 的额度">刷新</button>`:''}</strong>${(account.windows?.length ? account.windows.map(window=>[window.label,window]) : account.provider==='codex' ? [['额度窗口',null]] : [['5 小时',account.fiveHour],['7 天',account.sevenDay]]).map(([label,window])=>{
    if(!window) return `<div class="quota-window"><div><span>${label}</span><span class="muted">暂无数据</span></div></div>`;
    const source=window.source==='codex-log'?'Codex 本机记录':window.source==='desktop'?'桌面端本机记录':'CLI 本机缓存';
    const detail=window.stale ? `上次已用 ${window.usedPercent}% · ${dateTime(window.measuredAt)} · ${source}`:`${source} · ${window.ageMinutes} 分钟前`;
    const reset=window.resetAt ? `重置：${dateTime(Date.parse(window.resetAt)/1000)}`:'重置时间未知';
    return `<div class="quota-window${window.stale ? ' stale':''}"><div><span>${label}</span><strong>${window.stale ? (window.resetPassed ? '重置时刻已过':'快照已过期'):window.usedPercent+'% 已用'}</strong></div>${window.stale ? '':`<meter min="0" max="100" value="${window.usedPercent}" aria-label="${escapeHTML(account.name)} ${label}已用比例">${window.usedPercent}%</meter>`}<p>${escapeHTML(detail)}<br>${escapeHTML(reset)}</p></div>`;
  }).join('')}</div>`).join('') || '<p class="usage-note">暂无已登录账号。</p>';
  $('#usage-quality').textContent=`已去除 ${number(index.duplicates)} 条重复快照；${number(index.undatedRecords)} 条缺少有效时间，${number(index.emptyReplies)} 条没有 token 用量，${number(index.invalidLines)} 条日志行格式异常或过大。Codex 另有 ${number(data.codexQuality?.incomplete)} 条累计计数回退或会话标识缺失，${number(data.codexQuality?.failedFiles)} 个文件读取失败。`;
  $('#price-date').textContent=data.pricing.checkedAt;
  $('#price-sources').innerHTML=data.pricing.sources.map(source=>`<a href="${escapeHTML(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(source.name)}</a>`).join(' / ');
  $('#price-basis').textContent=data.provider==='claude' ? 'API 等价 token 费用，不是订阅账单；不含工具等额外费用。':data.pricing.basis;
}
function renderChart() {
  if(!usageData) return;
  const metric=$('#usage-metric').value, hourly=usageData.days===1, days=hourly ? usageData.hourly:usageData.daily;
  const value=row=>metric==='usd' ? (row.estimatedUsd || 0):row.totalTokens;
  const max=Math.max(...days.map(value),0) || 1, width=720, height=170, start=52, bottom=137, usable=width-start-8, step=usable/days.length;
  const tick=v=>metric==='usd' ? '$'+new Intl.NumberFormat('en-US',{maximumFractionDigits:max<.001?6:max<.01?4:2}).format(v):compact(v);
  let drawing=[0,.5,1].map(part=>{const y=bottom-part*110;return `<line x1="${start}" x2="${width}" y1="${y}" y2="${y}" class="chart-grid"/><text x="${start-9}" y="${y+3}" text-anchor="end" class="chart-tick">${escapeHTML(tick(max*part))}</text>`;}).join('');
  drawing+=days.map((row,i)=>{const v=value(row),barHeight=v/max*110,x=start+i*step+step*.18,w=step*.64,y=bottom-barHeight,label=hourly?String(row.hour).padStart(2,'0')+':00':row.date.slice(5),show=days.length<=7 || i%(hourly?4:5)===0 || i===days.length-1;
    return `<g><title>${hourly?label:row.date} · ${number(row.totalTokens)} Token · ${money(row.estimatedUsd)}${row.unpricedRequests ? ' · 部分未计价':''}</title><rect x="${x}" y="${y}" width="${w}" height="${barHeight}" rx="2" class="chart-bar"/>${show ? `<text x="${x+w/2}" y="158" text-anchor="middle" class="chart-tick">${label}</text>`:''}</g>`;}).join('');
  $('#usage-chart').innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="所选区间${hourly?'按小时':'按日'}${metric==='usd'?'API 等价费用':'Token'}趋势"><title>按本机日期统计，可在下方模型和账号明细中查看汇总。</title>${drawing}</svg>`;
  $('#chart-caption').textContent=`${number(usageData.summary.unpricedRequests)} 条未计价，未计入费用趋势。`;
  $('#chart-caption').hidden=metric!=='usd' || !usageData.summary.unpricedRequests;
}
async function loadUsage(refresh=false) {
  clearTimeout(usageTimer); const sequence=++usageSequence, days=usageDays;
  const params={days,provider:usageProvider};
  if(usagePeriod) params.period=usagePeriod;
  if(['current','previous'].includes(usagePeriod)) params.billingDay=Number($('#billing-day').value);
  if(usagePeriod==='custom') { params.start=$('#range-start').value; params.end=$('#range-end').value; }

  try {
    const data=await api(refresh ? '/api/usage/refresh':'/api/usage?'+new URLSearchParams(params),refresh ? params:undefined);
    if(sequence!==usageSequence) return;
    usageData=data; renderUsage();
  } catch(error) { if(sequence===usageSequence) { for(const id of ['usage-error','quota-error']) { $('#'+id).textContent=error.message; $('#'+id).hidden=false; } } }
  if(sequence===usageSequence && ['usage','quota'].includes(activePage) && !document.hidden) usageTimer=setTimeout(()=>loadUsage(),usageData?.index.scanning || !usageData?.index.lastScannedAt ? 1500:60000);
}
const rangeGroup=$('.usage-toolbar .range-buttons');
rangeGroup.addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button)return;
  usagePeriod=button.dataset.period || null;
  if(button.dataset.days) usageDays=Number(button.dataset.days);
  rangeGroup.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
  const custom=usagePeriod==='custom', billing=['current','previous'].includes(usagePeriod);
  $('#billing-day-label').hidden=!billing;
  $('#range-start-label').hidden=!custom;$('#range-end-label').hidden=!custom;
  $('#range-apply').hidden=!(custom||billing);
  if(custom) {
    const localDay=new Date(), today=new Date(localDay.getTime()-localDay.getTimezoneOffset()*60000).toISOString().slice(0,10);
    for(const id of ['range-start','range-end']) { $('#'+id).max=today;if(!$('#'+id).value) $('#'+id).value=usageData?.range?.[id==='range-start'?'start':'end'] || today; }
  }
  loadUsage();
});
$('#usage-range-form').addEventListener('submit',event=>{
  event.preventDefault();
  try { localStorage.setItem('usage-billing-day',$('#billing-day').value); } catch {}
  loadUsage();
});
$('#usage-metric').addEventListener('change',renderChart);
$('#usage-refresh').addEventListener('click',async event=>{const button=event.currentTarget;button.disabled=true;try{await loadUsage(true);}finally{button.disabled=false;}});
document.querySelectorAll('.provider-buttons').forEach(group=>group.addEventListener('click',event=>{const button=event.target.closest('button[data-provider]');if(!button)return;usageProvider=button.dataset.provider;document.querySelectorAll('[data-provider]').forEach(other=>other.setAttribute('aria-pressed',String(other.dataset.provider===usageProvider)));loadUsage();}));
document.querySelector('.account-provider').addEventListener('click',async event=>{const button=event.target.closest('[data-account-provider]');if(!button || busy)return;accountProvider=button.dataset.accountProvider;document.querySelectorAll('[data-account-provider]').forEach(other=>other.setAttribute('aria-pressed',String(other===button)));toggleAdd(false);await load();});
async function refreshClaudeQuota(id) {
  if(quotaRefreshBusy) return;
  if(!id) { toast('没有找到要查询的账号。',true); return; }
  quotaRefreshBusy=true; renderUsage();
  const status=$('#quota-query-status');status.hidden=false;status.textContent='正在运行官方 CLI /usage…';
  try {
    const result=await api('/api/quota/refresh',{id});
    await loadUsage();
    status.textContent=`${result.name} · 已更新 · ${result.cwd}`;
    toast('已通过官方 CLI 更新额度。');
  } catch(error) { status.textContent=error.message; toast(error.message,true); }
  finally { quotaRefreshBusy=false; if(usageData) renderUsage(); }
}
$('#quota-refresh').addEventListener('click',async event=>{
  const button=event.currentTarget;button.disabled=true;try{await loadUsage(true);}finally{button.disabled=false;}
});
$('#quota-rows').addEventListener('click',event=>{const button=event.target.closest('[data-quota-account]');if(button)refreshClaudeQuota(button.dataset.quotaAccount);});
function selectUsageTab(button) {document.querySelectorAll('.usage-tabs button').forEach(b=>{const selected=b===button;b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;$('#'+b.dataset.panel+'-panel').hidden=!selected;});}
$('.usage-tabs').addEventListener('click',event=>{const button=event.target.closest('button[data-panel]');if(button)selectUsageTab(button);});
$('.usage-tabs').addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;const tabs=[...document.querySelectorAll('.usage-tabs button')],i=tabs.indexOf(document.activeElement);if(i<0)return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(i+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;selectUsageTab(tabs[next]);tabs[next].focus();});
function selectPage(name, updateHash=false) {
  const button=document.querySelector(`.page-nav button[data-page="${name}"]`);
  if(!button) return;
  const focusWasInPanel=[...document.querySelectorAll('main > .page')].some(panel=>panel.contains(document.activeElement));
  activePage=name;
  document.title=button.textContent.trim()+' · Claude Accounts';
  document.querySelectorAll('.page-nav button').forEach(tab=>{const selected=tab===button;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;$('#'+tab.getAttribute('aria-controls')).hidden=!selected;});
  if(updateHash && location.hash!==`#${name}`) history.pushState(null,'',`#${name}`);
  if(focusWasInPanel) button.focus();
  clearTimeout(usageTimer);
  if(['usage','quota'].includes(name)) loadUsage();
}
function pageFromHash() {const name=location.hash.slice(1);selectPage(['accounts','usage','quota','connection'].includes(name)?name:'accounts');}
$('.page-nav').addEventListener('click',event=>{const button=event.target.closest('button[data-page]');if(button)selectPage(button.dataset.page,true);});
$('.page-nav').addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;const tabs=[...document.querySelectorAll('.page-nav button')],index=tabs.indexOf(document.activeElement);if(index<0)return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;selectPage(tabs[next].dataset.page,true);tabs[next].focus();});
window.addEventListener('popstate',pageFromHash);
window.addEventListener('hashchange',pageFromHash);
document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(usageTimer);else if(['usage','quota'].includes(activePage))loadUsage();});
pageFromHash();
