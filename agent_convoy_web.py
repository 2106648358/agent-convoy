"""Local-only HTML history center for Agent Convoy."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import agent_convoy as ac


LOG = logging.getLogger("agent-convoy.web")
MAX_BODY = 64 * 1024
MAX_PREVIEW_MESSAGES = 500
PLAN_TTL_SECONDS = 10 * 60


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>Agent Convoy · 本地会话历史中心</title>
  <link rel="stylesheet" href="/styles.css">
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div class="brand-mark">AC</div>
      <div class="brand-copy">
        <h1>Agent Convoy</h1>
        <p>本地会话历史中心</p>
      </div>
      <div class="privacy-pill"><span></span> 仅在本机运行</div>
      <button id="refreshAll" class="icon-button" type="button" title="刷新">↻</button>
    </header>

    <nav class="tabs" aria-label="主导航">
      <button class="tab active" data-tab="history" type="button">会话</button>
      <button class="tab" data-tab="ledger" type="button">操作记录</button>
      <button class="tab" data-tab="doctor" type="button">环境诊断</button>
    </nav>

    <main>
      <section id="historyTab" class="tab-panel active">
        <div class="history-grid">
          <aside class="session-panel panel">
            <div class="panel-heading">
              <div><p class="eyebrow">LOCAL SOURCES</p><h2>历史会话</h2></div>
              <span id="sessionCount" class="count-badge">0</span>
            </div>
            <div class="source-switcher" role="group" aria-label="来源">
              <button class="source-button active" data-source="all" type="button">全部</button>
              <button class="source-button" data-source="codex" type="button">Codex</button>
              <button class="source-button" data-source="claude" type="button">Claude</button>
              <button class="source-button" data-source="workbuddy" type="button">WorkBuddy</button>
            </div>
            <label class="field search-field"><span>搜索</span><input id="query" type="search" placeholder="标题、ID 或工作目录"></label>
            <label class="field"><span>开始日期</span><input id="since" type="date"></label>
            <div id="sessionList" class="session-list" aria-live="polite"></div>
          </aside>

          <section class="preview-panel panel">
            <div id="emptyPreview" class="empty-state">
              <div class="empty-icon">↗</div><h2>选择一条会话</h2>
              <p>在这里安全预览历史，然后选择完整迁移或 Markdown 降级导出。</p>
            </div>
            <div id="previewContent" class="hidden">
              <div class="preview-header">
                <div><p id="previewSource" class="eyebrow"></p><h2 id="previewTitle"></h2></div>
                <span id="previewMessageCount" class="count-badge"></span>
              </div>
              <dl class="meta-grid">
                <div><dt>会话 ID</dt><dd id="previewId"></dd></div>
                <div><dt>工作目录</dt><dd id="previewCwd"></dd></div>
                <div><dt>更新时间</dt><dd id="previewUpdated"></dd></div>
              </dl>
              <div id="messages" class="messages"></div>
            </div>
          </section>

          <aside class="action-panel panel">
            <div class="panel-heading"><div><p class="eyebrow">ACTIONS</p><h2>处理方式</h2></div></div>
            <section class="action-card">
              <div class="action-title"><span class="action-number">1</span><div><h3>完整迁移</h3><p>创建目标端会话</p></div></div>
              <label class="field"><span>目标端</span><select id="target"><option value="workbuddy">WorkBuddy</option><option value="codex">Codex</option><option value="claude">Claude</option></select></label>
              <div id="migrationWarning" class="warning">WorkBuddy 写入为实验性，可能不会进入原生图形历史列表。请保留 target ID。</div>
              <button id="planMigration" class="button secondary" type="button" disabled>先预演迁移</button>
              <label id="migrationConfirmWrap" class="confirm hidden"><input id="migrationConfirm" type="checkbox"> 我已核对预演结果</label>
              <button id="applyMigration" class="button primary hidden" type="button">确认并迁移</button>
            </section>
            <div class="or"><span>OR</span></div>
            <section class="action-card reliable">
              <div class="action-title"><span class="action-number">2</span><div><h3>导出 Markdown</h3><p>可靠降级路径</p></div></div>
              <label class="field"><span>导出目录</span><input id="outputDir" type="text" placeholder="请选择文件夹" readonly></label>
              <button id="chooseDirectory" class="button secondary" type="button">选择目录…</button>
              <button id="exportMarkdown" class="button accent" type="button" disabled>导出可读完整记录</button>
              <button id="revealExport" class="button secondary hidden" type="button">在 Finder 中显示</button>
            </section>
            <pre id="operationResult" class="operation-result" aria-live="polite"></pre>
          </aside>
        </div>
      </section>

      <section id="ledgerTab" class="tab-panel"><div class="wide-panel panel"><div class="panel-heading"><div><p class="eyebrow">AUDIT TRAIL</p><h2>操作记录</h2></div><button id="refreshLedger" class="button secondary compact" type="button">刷新</button></div><div id="ledgerContent"></div></div></section>
      <section id="doctorTab" class="tab-panel"><div class="wide-panel panel"><div class="panel-heading"><div><p class="eyebrow">ENVIRONMENT</p><h2>环境诊断</h2></div><button id="refreshDoctor" class="button secondary compact" type="button">重新检测</button></div><div id="doctorContent" class="doctor-grid"></div></div></section>
    </main>
    <footer>Agent Convoy 不会直接修改 Codex SQLite 或 WorkBuddy 旧版索引。</footer>
  </div>
  <script src="/app.js" defer></script>
</body>
</html>
"""


STYLES_CSS = r"""
:root{--bg:#0d1117;--panel:#151b23;--panel-2:#1b2330;--border:#2a3442;--text:#eef3f8;--muted:#93a1b2;--accent:#75e0bd;--accent-2:#54b99b;--blue:#78a9ff;--warn:#f4c76b;--danger:#ff8f8f;--shadow:0 18px 50px rgba(0,0,0,.24);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0%,#172331 0,transparent 32%),var(--bg);color:var(--text);min-height:100vh}.shell{max-width:1680px;margin:0 auto;padding:0 28px 28px}.topbar{height:82px;display:flex;align-items:center;gap:14px;border-bottom:1px solid var(--border)}.brand-mark{width:42px;height:42px;display:grid;place-items:center;border-radius:12px;background:linear-gradient(135deg,var(--accent),#8fbaff);color:#08221b;font-weight:900;letter-spacing:-1px}.brand-copy h1{font-size:18px;margin:0}.brand-copy p{font-size:12px;color:var(--muted);margin:3px 0 0}.privacy-pill{margin-left:auto;padding:8px 12px;border:1px solid #285846;background:#14271f;border-radius:999px;color:#a6efd5;font-size:12px}.privacy-pill span{display:inline-block;width:7px;height:7px;background:var(--accent);border-radius:50%;margin-right:6px;box-shadow:0 0 12px var(--accent)}.icon-button{border:1px solid var(--border);background:var(--panel);color:var(--text);width:36px;height:36px;border-radius:10px;font-size:20px;cursor:pointer}.tabs{display:flex;gap:4px;padding:18px 0}.tab{border:0;background:transparent;color:var(--muted);padding:10px 17px;border-radius:9px;font-weight:700;cursor:pointer}.tab.active{background:var(--panel-2);color:var(--text)}.tab-panel{display:none}.tab-panel.active{display:block}.history-grid{display:grid;grid-template-columns:minmax(290px,340px) minmax(420px,1fr) minmax(300px,360px);gap:16px;min-height:calc(100vh - 180px)}.panel{background:rgba(21,27,35,.96);border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow)}.session-panel,.action-panel{padding:18px;overflow:hidden}.preview-panel{padding:22px;overflow:auto;max-height:calc(100vh - 180px)}.panel-heading,.preview-header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.16em;color:var(--accent);margin:0 0 5px}.panel h2{font-size:20px;margin:0}.count-badge{border:1px solid var(--border);background:var(--panel-2);padding:5px 9px;border-radius:999px;color:var(--muted);font-size:12px}.source-switcher{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;background:#10151c;border-radius:10px;padding:4px;margin-bottom:14px}.source-button{border:0;background:transparent;color:var(--muted);font-size:11px;padding:8px 3px;border-radius:7px;cursor:pointer}.source-button.active{background:var(--panel-2);color:var(--text)}.field{display:block;margin:11px 0}.field span{display:block;color:var(--muted);font-size:11px;font-weight:700;margin-bottom:6px}.field input,.field select{width:100%;border:1px solid var(--border);background:#0f141b;color:var(--text);border-radius:9px;padding:10px 11px;outline:none}.field input:focus,.field select:focus{border-color:var(--accent-2)}.session-list{margin-top:13px;display:flex;flex-direction:column;gap:6px;max-height:calc(100vh - 420px);overflow:auto;padding-right:4px}.session-row{width:100%;text-align:left;border:1px solid transparent;background:transparent;color:var(--text);border-radius:10px;padding:11px;cursor:pointer}.session-row:hover{background:var(--panel-2)}.session-row.active{border-color:#3c806b;background:#1a2b27}.session-row strong{display:block;font-size:13px;line-height:1.35}.session-row small{display:flex;justify-content:space-between;color:var(--muted);font-size:10px;margin-top:7px}.source-tag{color:var(--blue);text-transform:uppercase;font-weight:800}.empty-state{min-height:520px;display:grid;place-content:center;text-align:center;max-width:420px;margin:auto}.empty-icon{width:58px;height:58px;border:1px solid var(--border);border-radius:18px;display:grid;place-items:center;margin:0 auto 15px;color:var(--accent);font-size:25px}.empty-state p{color:var(--muted);line-height:1.6}.hidden{display:none!important}.preview-header h2{font-size:24px;line-height:1.25}.meta-grid{display:grid;grid-template-columns:1fr 1.2fr .8fr;gap:10px;margin:0 0 18px}.meta-grid div{background:#10151c;border-radius:10px;padding:10px;min-width:0}.meta-grid dt{color:var(--muted);font-size:10px;margin-bottom:5px}.meta-grid dd{margin:0;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.messages{display:flex;flex-direction:column;gap:12px}.message{border:1px solid var(--border);border-radius:12px;padding:14px;background:#10151c}.message.user{border-left:3px solid var(--blue)}.message.assistant{border-left:3px solid var(--accent)}.message.tool{border-left:3px solid var(--warn)}.message-head{display:flex;justify-content:space-between;color:var(--muted);font-size:10px;text-transform:uppercase;font-weight:800;margin-bottom:9px}.message-body{white-space:pre-wrap;word-break:break-word;font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}.action-card{padding:14px;background:#10151c;border:1px solid var(--border);border-radius:12px}.action-card.reliable{border-color:#285846}.action-title{display:flex;gap:10px;align-items:center;margin-bottom:10px}.action-number{display:grid;place-items:center;width:27px;height:27px;border-radius:8px;background:var(--panel-2);color:var(--accent);font-weight:900}.action-title h3{font-size:14px;margin:0}.action-title p{font-size:10px;color:var(--muted);margin:3px 0 0}.warning{font-size:11px;line-height:1.5;color:#ffe0a0;background:#2d2514;border:1px solid #584824;padding:9px;border-radius:8px;margin:9px 0}.button{width:100%;border:1px solid transparent;border-radius:9px;padding:10px 12px;font-weight:800;cursor:pointer;margin-top:8px}.button:disabled{opacity:.45;cursor:not-allowed}.button.primary{background:var(--blue);color:#0b1729}.button.accent{background:var(--accent);color:#08221b}.button.secondary{background:var(--panel-2);border-color:var(--border);color:var(--text)}.button.compact{width:auto;margin:0;padding:8px 12px}.confirm{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:11px;margin-top:10px}.or{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:9px;margin:10px 0}.or:before,.or:after{content:"";height:1px;background:var(--border);flex:1}.operation-result{white-space:pre-wrap;max-height:180px;overflow:auto;color:#bad0e6;background:#0c1117;border-radius:9px;padding:10px;font-size:10px;margin-top:12px;min-height:38px}.wide-panel{padding:22px;min-height:calc(100vh - 200px)}.ledger-section h3{font-size:14px;color:var(--accent);margin:24px 0 10px}.record-card{display:grid;grid-template-columns:140px 1fr auto;gap:12px;align-items:center;padding:12px;border-bottom:1px solid var(--border)}.record-card strong{font-size:12px}.record-card p{font-size:11px;color:var(--muted);margin:3px 0;word-break:break-all}.status{font-size:10px;padding:5px 8px;border-radius:999px;background:var(--panel-2)}.record-actions{display:flex;gap:6px}.mini-button{border:1px solid var(--border);background:var(--panel-2);color:var(--text);border-radius:7px;padding:7px 9px;font-size:10px;cursor:pointer}.doctor-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.doctor-card{background:#10151c;border:1px solid var(--border);border-radius:13px;padding:16px}.doctor-card h3{text-transform:capitalize;margin:0 0 12px}.doctor-card dl{margin:0}.doctor-card div{margin:8px 0}.doctor-card dt{font-size:10px;color:var(--muted)}.doctor-card dd{font-size:11px;margin:3px 0;word-break:break-word}footer{text-align:center;color:#657486;font-size:10px;padding:18px}.error{color:var(--danger)}.success{color:var(--accent)}@media(max-width:1100px){.history-grid{grid-template-columns:300px 1fr}.action-panel{grid-column:1/-1}.session-list{max-height:520px}.preview-panel{max-height:none}.doctor-grid{grid-template-columns:1fr 1fr}}@media(max-width:760px){.shell{padding:0 12px 18px}.privacy-pill{display:none}.history-grid{grid-template-columns:1fr}.meta-grid{grid-template-columns:1fr}.session-list{max-height:360px}.doctor-grid{grid-template-columns:1fr}.record-card{grid-template-columns:1fr}.topbar{height:72px}}
"""


APP_JS = r"""
const state={source:'all',sessions:[],selected:null,planId:null,exportId:null,token:null};
const $=id=>document.getElementById(id);
const roleNames={user:'用户',assistant:'助手',tool:'工具结果',system:'系统'};
function initToken(){const u=new URL(location.href);state.token=u.searchParams.get('token')||sessionStorage.getItem('agentConvoyToken');if(state.token){sessionStorage.setItem('agentConvoyToken',state.token);u.searchParams.delete('token');history.replaceState({},'',u.pathname+u.search)}if(!state.token)throw new Error('缺少本机访问令牌');}
async function api(path,options={}){const headers={'X-Agent-Convoy-Token':state.token,...(options.headers||{})};if(options.body)headers['Content-Type']='application/json';const r=await fetch(path,{...options,headers});let data={};try{data=await r.json()}catch{}if(!r.ok)throw new Error(data.error||`请求失败 ${r.status}`);return data;}
function text(tag,value,cls){const n=document.createElement(tag);n.textContent=value??'';if(cls)n.className=cls;return n;}
function showResult(value,error=false){const el=$('operationResult');el.textContent=typeof value==='string'?value:JSON.stringify(value,null,2);el.className=`operation-result ${error?'error':'success'}`;}
function setBusy(button,busy,label){button.disabled=busy;if(label)button.textContent=busy?'处理中…':label;}
function switchTab(name){document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.tab===name));document.querySelectorAll('.tab-panel').forEach(x=>x.classList.remove('active'));$(`${name}Tab`).classList.add('active');if(name==='ledger')loadLedger();if(name==='doctor')loadDoctor();}
async function loadSessions(){const list=$('sessionList');list.replaceChildren(text('p','正在扫描本地会话…','empty-note'));const params=new URLSearchParams({source:state.source,query:$('query').value,since:$('since').value,limit:'500'});try{const data=await api(`/api/sessions?${params}`);state.sessions=data.sessions;$('sessionCount').textContent=String(data.sessions.length);renderSessions();}catch(e){list.replaceChildren(text('p',e.message,'error'));}}
function renderSessions(){const list=$('sessionList');list.replaceChildren();if(!state.sessions.length){list.append(text('p','没有匹配的会话。','empty-note'));return;}for(const item of state.sessions){const b=document.createElement('button');b.type='button';b.className='session-row'+(state.selected&&state.selected.id===item.id&&state.selected.source===item.source?' active':'');const title=text('strong',item.title);const meta=document.createElement('small');meta.append(text('span',item.source,'source-tag'),text('span',`${item.message_count} 条 · ${(item.updated_at||'-').slice(0,10)}`));b.append(title,meta);b.addEventListener('click',()=>selectSession(item));list.append(b);}}
async function selectSession(item){try{const data=await api(`/api/sessions/${encodeURIComponent(item.source)}/${encodeURIComponent(item.id)}?limit=300`);state.selected=data;state.planId=null;state.exportId=null;$('emptyPreview').classList.add('hidden');$('previewContent').classList.remove('hidden');$('previewSource').textContent=data.source.toUpperCase();$('previewTitle').textContent=data.title;$('previewId').textContent=data.id;$('previewCwd').textContent=data.cwd;$('previewUpdated').textContent=data.updated_at||'-';$('previewMessageCount').textContent=`${data.total_messages} 条`;$('messages').replaceChildren();data.messages.forEach((m,i)=>{const card=document.createElement('article');card.className=`message ${m.role}`;const head=document.createElement('div');head.className='message-head';head.append(text('span',`${i+1}. ${roleNames[m.role]||m.role}`),text('span',m.created_at||''));const body=text('div',m.content,'message-body');card.append(head,body);$('messages').append(card);});$('planMigration').disabled=false;$('exportMarkdown').disabled=!$('outputDir').value;$('migrationConfirmWrap').classList.add('hidden');$('applyMigration').classList.add('hidden');$('revealExport').classList.add('hidden');updateWarning();renderSessions();showResult('已加载会话，可选择完整迁移或 Markdown 降级。');}catch(e){showResult(e.message,true);}}
function updateWarning(){const target=$('target').value;const box=$('migrationWarning');if(target==='workbuddy'){box.textContent='WorkBuddy 写入为实验性，可能不会进入原生图形历史列表。请保留 target ID。';box.classList.remove('hidden');}else if(target==='codex'&&state.selected?.source!=='claude'){box.textContent='非 Claude 来源写入 Codex 后可按 ID 恢复，但首次真实 turn 前可能不显示在历史列表。';box.classList.remove('hidden');}else box.classList.add('hidden');}
async function planMigration(){if(!state.selected)return;const target=$('target').value;if(target===state.selected.source){showResult('来源端和目标端不能相同。',true);return;}const b=$('planMigration');setBusy(b,true,'先预演迁移');try{const data=await api('/api/migrate/plan',{method:'POST',body:JSON.stringify({source:state.selected.source,target,session_id:state.selected.id})});state.planId=data.plan_id;showResult(data);$('migrationConfirmWrap').classList.remove('hidden');$('applyMigration').classList.remove('hidden');$('migrationConfirm').checked=false;$('applyMigration').disabled=true;}catch(e){showResult(e.message,true);}finally{setBusy(b,false,'先预演迁移');}}
async function applyMigration(){if(!state.planId||!$('migrationConfirm').checked)return;const b=$('applyMigration');setBusy(b,true,'确认并迁移');try{const data=await api('/api/migrate/apply',{method:'POST',body:JSON.stringify({plan_id:state.planId,confirm:true})});showResult(data);state.planId=null;$('migrationConfirmWrap').classList.add('hidden');b.classList.add('hidden');await loadLedger();}catch(e){showResult(e.message,true);}finally{setBusy(b,false,'确认并迁移');}}
async function chooseDirectory(){const b=$('chooseDirectory');setBusy(b,true,'选择目录…');try{const data=await api('/api/choose-directory',{method:'POST',body:'{}'});if(!data.cancelled){$('outputDir').value=data.path;$('exportMarkdown').disabled=!state.selected;}}catch(e){showResult(e.message,true);}finally{setBusy(b,false,'选择目录…');}}
async function exportMarkdown(){if(!state.selected||!$('outputDir').value)return;const b=$('exportMarkdown');setBusy(b,true,'导出可读完整记录');try{const data=await api('/api/export',{method:'POST',body:JSON.stringify({source:state.selected.source,session_id:state.selected.id,output_dir:$('outputDir').value})});state.exportId=data.export_id;showResult(data);$('revealExport').classList.remove('hidden');await loadLedger();}catch(e){showResult(e.message,true);}finally{setBusy(b,false,'导出可读完整记录');}}
async function revealExport(id=state.exportId){if(!id)return;try{const data=await api('/api/reveal',{method:'POST',body:JSON.stringify({export_id:id})});showResult(data);}catch(e){showResult(e.message,true);}}
async function loadLedger(){const root=$('ledgerContent');root.replaceChildren(text('p','正在读取记录…'));try{const data=await api('/api/ledger');root.replaceChildren();const imports=document.createElement('section');imports.className='ledger-section';imports.append(text('h3',`迁移记录 · ${data.imports.length}`));data.imports.slice().reverse().forEach(r=>imports.append(recordCard(r,'import')));const exports=document.createElement('section');exports.className='ledger-section';exports.append(text('h3',`Markdown 导出 · ${data.exports.length}`));data.exports.slice().reverse().forEach(r=>exports.append(recordCard(r,'export')));root.append(imports,exports);}catch(e){root.replaceChildren(text('p',e.message,'error'));}}
function recordCard(r,kind){const card=document.createElement('div');card.className='record-card';const left=document.createElement('div');left.append(text('strong',kind==='import'?`${r.source} → ${r.target}`:r.source),text('p',(r.created_at||'').slice(0,19)));const middle=document.createElement('div');middle.append(text('strong',r.title||r.source_id||r.import_id),text('p',kind==='import'?`目标：${r.target_id||'-'}`:r.path));const actions=document.createElement('div');actions.className='record-actions';actions.append(text('span',r.status,'status'));if(kind==='export'&&r.status==='created'){const b=text('button','Finder','mini-button');b.type='button';b.addEventListener('click',()=>revealExport(r.export_id));actions.append(b);}if(kind==='import'&&r.status==='applied'){const b=text('button','预演回滚','mini-button');b.type='button';b.addEventListener('click',()=>planRollback(r.import_id,b));actions.append(b);}card.append(left,middle,actions);return card;}
async function planRollback(importId,button){try{const data=await api('/api/rollback/plan',{method:'POST',body:JSON.stringify({import_id:importId})});button.textContent='确认回滚';button.classList.add('danger');button.onclick=null;button.addEventListener('click',()=>applyRollback(data.plan_id));alert(`回滚预演完成：\n${JSON.stringify(data.plan,null,2)}\n\n再次点击“确认回滚”执行。`);}catch(e){alert(e.message);}}
async function applyRollback(planId){if(!confirm('确定回滚这次导入？仅删除该次新建的目标会话。'))return;try{await api('/api/rollback/apply',{method:'POST',body:JSON.stringify({plan_id:planId,confirm:true})});await loadLedger();}catch(e){alert(e.message);}}
async function loadDoctor(){const root=$('doctorContent');root.replaceChildren(text('p','正在检测…'));try{const data=await api('/api/doctor');root.replaceChildren();for(const [name,value] of Object.entries(data)){const card=document.createElement('article');card.className='doctor-card';card.append(text('h3',name));const dl=document.createElement('dl');for(const [k,v] of Object.entries(value)){const row=document.createElement('div');row.append(text('dt',k),text('dd',typeof v==='object'?JSON.stringify(v):String(v)));dl.append(row);}card.append(dl);root.append(card);}}catch(e){root.replaceChildren(text('p',e.message,'error'));}}
function debounce(fn,ms){let t;return(...args)=>{clearTimeout(t);t=setTimeout(()=>fn(...args),ms)}}
async function refreshAll(){await Promise.all([loadSessions(),loadDoctor(),loadLedger()]);}
document.addEventListener('DOMContentLoaded',async()=>{try{initToken();document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>switchTab(b.dataset.tab)));document.querySelectorAll('.source-button').forEach(b=>b.addEventListener('click',()=>{state.source=b.dataset.source;document.querySelectorAll('.source-button').forEach(x=>x.classList.toggle('active',x===b));loadSessions();}));$('query').addEventListener('input',debounce(loadSessions,250));$('since').addEventListener('change',loadSessions);$('target').addEventListener('change',updateWarning);$('planMigration').addEventListener('click',planMigration);$('migrationConfirm').addEventListener('change',e=>$('applyMigration').disabled=!e.target.checked);$('applyMigration').addEventListener('click',applyMigration);$('chooseDirectory').addEventListener('click',chooseDirectory);$('exportMarkdown').addEventListener('click',exportMarkdown);$('revealExport').addEventListener('click',()=>revealExport());$('refreshAll').addEventListener('click',refreshAll);$('refreshLedger').addEventListener('click',loadLedger);$('refreshDoctor').addEventListener('click',loadDoctor);await refreshAll();}catch(e){document.body.replaceChildren(text('pre',e.message,'error'));}});
"""


class AppState:
    def __init__(self, paths: ac.Paths, token: Optional[str] = None):
        self.paths = paths
        self.token = token or secrets.token_urlsafe(32)
        self.origin = ""
        self.plans: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def create_plan(self, kind: str, payload: dict[str, Any], result: dict[str, Any]) -> str:
        plan_id = str(uuid.uuid4())
        with self.lock:
            self.plans[plan_id] = {"kind": kind, "payload": payload, "result": result, "expires": time.time() + PLAN_TTL_SECONDS}
        return plan_id

    def consume_plan(self, plan_id: str, kind: str) -> dict[str, Any]:
        with self.lock:
            plan = self.plans.pop(plan_id, None)
        if not plan or plan.get("kind") != kind:
            raise ac.ConvoyError("Confirmation plan not found or already used")
        if float(plan.get("expires", 0)) < time.time():
            raise ac.ConvoyError("Confirmation plan expired; run the preview again")
        return plan


def choose_directory() -> Optional[str]:
    if sys.platform != "darwin":
        raise ac.ConvoyError("The system directory picker is currently supported on macOS only")
    script = 'POSIX path of (choose folder with prompt "选择 Markdown 导出目录")'
    result = subprocess.run(["/usr/bin/osascript", "-e", script], text=True, capture_output=True, timeout=120, check=False)
    if result.returncode:
        if "User canceled" in result.stderr or "-128" in result.stderr:
            return None
        raise ac.ConvoyError("Could not open the macOS directory picker: " + ac.sanitize_diagnostic(result.stderr))
    selected = result.stdout.strip()
    return str(ac.validate_export_directory(Path(selected))) if selected else None


def conversation_payload(paths: ac.Paths, source: str, session_id: str, limit: int, offset: int = 0) -> dict[str, Any]:
    conversation = ac.adapter_for(source, paths).load(session_id)
    messages = []
    for message in conversation.messages[offset : offset + limit]:
        content = ac.readable_message_content(message)
        if not content:
            continue
        messages.append({"role": message.role, "content": content[:20_000], "created_at": message.created_at})
    return {
        "id": conversation.id,
        "source": conversation.source,
        "title": conversation.title,
        "cwd": conversation.cwd,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "messages": messages,
        "total_messages": len(conversation.messages),
        "offset": offset,
        "limit": limit,
    }


def handler_factory(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentConvoy/0.2"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.debug("http client=%s " + fmt, self.client_address[0], *args)

        def _json(self, status: int, payload: Any) -> None:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def _asset(self, content_type: str, value: str) -> None:
            raw = value.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self, *, require_origin: bool = False) -> bool:
            supplied = self.headers.get("X-Agent-Convoy-Token", "")
            if not supplied or not hmac.compare_digest(supplied, state.token):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid local capability token"})
                return False
            if require_origin and self.headers.get("Origin") != state.origin:
                self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid request origin"})
                return False
            return True

        def _read_body(self) -> dict[str, Any]:
            if self.headers.get_content_type() != "application/json":
                raise ac.ConvoyError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ac.ConvoyError("Invalid Content-Length") from exc
            if length < 0 or length > MAX_BODY:
                raise ac.ConvoyError("Request body is too large")
            try:
                value = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                raise ac.ConvoyError("Invalid JSON body") from exc
            if not isinstance(value, dict):
                raise ac.ConvoyError("JSON body must be an object")
            return value

        @staticmethod
        def _required_string(body: dict[str, Any], name: str) -> str:
            value = body.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ac.ConvoyError(f"{name} is required")
            return value.strip()

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._asset("text/html; charset=utf-8", INDEX_HTML)
                return
            if parsed.path == "/styles.css":
                self._asset("text/css; charset=utf-8", STYLES_CSS)
                return
            if parsed.path == "/app.js":
                self._asset("application/javascript; charset=utf-8", APP_JS)
                return
            if not parsed.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            if not self._authorized():
                return
            try:
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/api/doctor":
                    self._json(HTTPStatus.OK, ac.doctor(state.paths))
                    return
                if parsed.path == "/api/ledger":
                    value = ac.Ledger(state.paths.state_home).read()
                    self._json(HTTPStatus.OK, {"version": value["version"], "imports": [ac.safe_public_record(x) for x in value["imports"]], "exports": [ac.safe_public_record(x) for x in value["exports"]]})
                    return
                if parsed.path == "/api/sessions":
                    source = query.get("source", ["all"])[0]
                    if source != "all" and source not in ac.SUPPORTED:
                        raise ac.ConvoyError("Unsupported source")
                    try:
                        limit = min(500, max(1, int(query.get("limit", ["200"])[0])))
                    except ValueError as exc:
                        raise ac.ConvoyError("Invalid limit") from exc
                    values = []
                    sources = ac.SUPPORTED if source == "all" else (source,)
                    for item_source in sources:
                        values.extend(ac.adapter_for(item_source, state.paths).list_sessions())
                    values = ac.filter_summaries(values, query.get("query", [None])[0], query.get("since", [None])[0])
                    values.sort(key=lambda item: item.updated_at or "", reverse=True)
                    payload = []
                    for item in values[:limit]:
                        record = dataclasses.asdict(item)
                        record["source"] = item.adapter.split("-")[0] if item.adapter else source
                        if item.adapter.startswith("workbuddy"):
                            record["source"] = "workbuddy"
                        elif item.adapter.startswith("codex"):
                            record["source"] = "codex"
                        elif item.adapter.startswith("claude"):
                            record["source"] = "claude"
                        record.pop("source_path", None)
                        payload.append(record)
                    self._json(HTTPStatus.OK, {"sessions": payload, "count": len(payload)})
                    return
                prefix = "/api/sessions/"
                if parsed.path.startswith(prefix):
                    parts = parsed.path[len(prefix) :].split("/", 1)
                    if len(parts) != 2:
                        raise ac.ConvoyError("Session source and id are required")
                    source, session_id = map(urllib.parse.unquote, parts)
                    if source not in ac.SUPPORTED:
                        raise ac.ConvoyError("Unsupported source")
                    try:
                        limit = min(MAX_PREVIEW_MESSAGES, max(1, int(query.get("limit", ["200"])[0])))
                        offset = max(0, int(query.get("offset", ["0"])[0]))
                    except ValueError as exc:
                        raise ac.ConvoyError("Invalid pagination") from exc
                    self._json(HTTPStatus.OK, conversation_payload(state.paths, source, session_id, limit, offset))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except (ac.ConvoyError, OSError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": ac.sanitize_diagnostic(str(exc))})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            if not self._authorized(require_origin=True):
                return
            try:
                body = self._read_body()
                if parsed.path == "/api/choose-directory":
                    selected = choose_directory()
                    self._json(HTTPStatus.OK, {"cancelled": selected is None, "path": selected})
                    return
                if parsed.path == "/api/export":
                    source = self._required_string(body, "source")
                    session_id = self._required_string(body, "session_id")
                    output_dir = self._required_string(body, "output_dir")
                    if source not in ac.SUPPORTED:
                        raise ac.ConvoyError("Unsupported source")
                    self._json(HTTPStatus.OK, ac.export_conversation(state.paths, source, session_id, Path(output_dir)))
                    return
                if parsed.path == "/api/reveal":
                    export_id = self._required_string(body, "export_id")
                    self._json(HTTPStatus.OK, ac.reveal_export(state.paths, export_id))
                    return
                if parsed.path == "/api/migrate/plan":
                    source = self._required_string(body, "source")
                    target = self._required_string(body, "target")
                    session_id = self._required_string(body, "session_id")
                    if source not in ac.SUPPORTED or target not in ac.SUPPORTED:
                        raise ac.ConvoyError("Unsupported source or target")
                    payload = {"source": source, "target": target, "session_id": session_id}
                    result = ac.perform_import(state.paths, source, target, session_id, apply=False, title=None, cwd=None)
                    plan_id = state.create_plan("migrate", payload, result)
                    self._json(HTTPStatus.OK, {"plan_id": plan_id, "expires_in_seconds": PLAN_TTL_SECONDS, "plan": result})
                    return
                if parsed.path == "/api/migrate/apply":
                    if body.get("confirm") is not True:
                        raise ac.ConvoyError("Explicit confirmation is required")
                    plan = state.consume_plan(self._required_string(body, "plan_id"), "migrate")
                    payload = plan["payload"]
                    result = ac.perform_import(state.paths, payload["source"], payload["target"], payload["session_id"], apply=True, title=None, cwd=None)
                    self._json(HTTPStatus.OK, result)
                    return
                if parsed.path == "/api/rollback/plan":
                    import_id = self._required_string(body, "import_id")
                    result = ac.rollback(state.paths, import_id, apply=False)
                    plan_id = state.create_plan("rollback", {"import_id": import_id}, result)
                    self._json(HTTPStatus.OK, {"plan_id": plan_id, "expires_in_seconds": PLAN_TTL_SECONDS, "plan": result})
                    return
                if parsed.path == "/api/rollback/apply":
                    if body.get("confirm") is not True:
                        raise ac.ConvoyError("Explicit confirmation is required")
                    plan = state.consume_plan(self._required_string(body, "plan_id"), "rollback")
                    self._json(HTTPStatus.OK, ac.rollback(state.paths, plan["payload"]["import_id"], apply=True))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except (ac.ConvoyError, OSError, subprocess.SubprocessError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": ac.sanitize_diagnostic(str(exc))})

        def do_PUT(self) -> None:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Method not allowed"})

        do_DELETE = do_PUT
        do_PATCH = do_PUT

    return Handler


def create_server(paths: ac.Paths, port: int = 0, token: Optional[str] = None) -> tuple[ThreadingHTTPServer, AppState]:
    state = AppState(paths, token=token)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_factory(state))
    actual_port = int(server.server_address[1])
    state.origin = f"http://127.0.0.1:{actual_port}"
    return server, state


def serve_ui(paths: ac.Paths, port: int = 0, open_browser: bool = True) -> None:
    server, state = create_server(paths, port=port)
    url = f"{state.origin}/?token={urllib.parse.quote(state.token)}"
    print(f"Agent Convoy HTML history center: {state.origin}")
    print("Press Ctrl-C to stop. The service only listens on 127.0.0.1.")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    else:
        print(f"Open this one-time local URL: {url}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping Agent Convoy UI.")
    finally:
        server.server_close()

