import {toggleRanges, isCovered, excerpt} from '/highlights.js';
const group = location.pathname.split('/').pop();
const $ = selector => document.querySelector(selector);
const views = {stance:$('#stance-view'), reading:$('#reading-view'), summary:$('#summary-view'), complete:$('#complete-view')};
let article, student, work, selected = [], timer, changes = 0, savedChanges = 0, queue = Promise.resolve(), submitting = false;
function error(message) { $('#error').textContent = message; $('#error').hidden = !message; }
async function api(path, payload) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 180000);
  try {
    const response = await fetch(path, {method:payload === undefined ? 'GET' : 'POST',
      credentials:'same-origin', headers:{'Content-Type':'application/json','X-Learning-Client':'1'},
      body:payload === undefined ? undefined : JSON.stringify(payload), signal:controller.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(response.status === 401
      ? '登入已失效。請保留此頁與推論文字，另開登入頁重新登入，再重試儲存。'
      : data.message || '操作未完成，請稍後重試。');
    return data;
  } catch (e) {
    if (e.name === 'AbortError' || e instanceof TypeError) throw new Error('連線中斷，尚未確認儲存。請保留此頁並稍後重試。');
    throw e;
  } finally { clearTimeout(timeout); }
}
function payload() {
  return {classroom:student.classroom, seat:student.seat, article_id:article.id,
    revision:work.revision, stance:work.stance, stage:work.stage,
    highlights:work.highlights, inference:work.inference};
}
function save() {
  clearTimeout(timer);
  const operation = queue.then(async () => {
    if (changes === savedChanges || work.submitted_at) return;
    const generation = changes;
    $('#save-status').textContent = '儲存中…';
    const result = await api(`/api/${group}/draft`, payload());
    work.revision = result.work.revision;
    work.attempt_id = result.work.attempt_id;
    savedChanges = generation;
    $('#save-status').textContent = '已暫存，等待每 35 秒同步';
    error('');
  });
  queue = operation.catch(e => { $('#save-status').textContent = '尚未儲存'; error(e.message); });
  return operation;
}
function changed() {
  changes++;
  $('#save-status').textContent = '尚未儲存';
  clearTimeout(timer);
  timer = setTimeout(() => save().catch(() => {}), 600);
}
function show(stage, focus = true) {
  for (const [key, view] of Object.entries(views)) view.hidden = key !== stage;
  document.querySelectorAll('[data-step]').forEach(item => {
    if (item.dataset.step === (stage === 'complete' ? 'summary' : stage)) item.setAttribute('aria-current','step');
    else item.removeAttribute('aria-current');
  });
  document.querySelectorAll('.stance-chip').forEach(el => { el.textContent = `我的立場：${work.stance || ''}`; });
  if (stage === 'reading') { syncArticle(); renderArticle(); updateChatControls(); }
  if (stage === 'summary') renderSummary();
  if (stage === 'complete') {
    $('#save-status').textContent = '已提交';
    $('#receipt').textContent = `提交時間：${new Date(work.submitted_at).toLocaleString('zh-TW')}　紀錄編號：${work.attempt_id.slice(0,8)}`;
  }
  if (focus) { window.scrollTo({top:0}); views[stage].querySelector('h1').focus({preventScroll:true}); }
}
function renderArticle() {
  const fragment = document.createDocumentFragment();
  article.paragraphs.forEach((paragraph,p) => {
    const element = document.createElement('p'); element.dataset.paragraph = String(p);
    element.className = 'bubble ' + (work.messages[p].role === 'user' ? 'student-bubble' : 'ai-bubble');
    const label = document.createElement('div'); label.className='speaker';
    label.textContent=work.messages[p].role === 'user' ? '你' : 'AI 學習夥伴';
    fragment.append(label);
    const marks = work.highlights.filter(h => h.p === p);
    let position = 0;
    for (const part of paragraph.parts) {
      const length = Array.from(part.text).length;
      const boundaries = new Set([position, position+length]);
      for (const h of marks) {
        if (h.start > position && h.start < position+length) boundaries.add(h.start);
        if (h.end > position && h.end < position+length) boundaries.add(h.end);
      }
      const points = [...boundaries].sort((a,b) => a-b);
      for (let i=0;i<points.length-1;i++) {
        const start=points[i], end=points[i+1];
        let node = document.createTextNode(excerpt(paragraph.text,start,end));
        if (part.bold) { const strong=document.createElement('strong'); strong.append(node); node=strong; }
        if (marks.some(h => h.start <= start && h.end >= end)) { const mark=document.createElement('mark'); mark.append(node); node=mark; }
        element.append(node);
      }
      position += length;
    }
    fragment.append(element);
  });
  $('#article').replaceChildren(fragment);
  if (!article.paragraphs.length) {const empty=document.createElement('div');empty.className='chat-empty';empty.textContent='從一個問題開始，一起探索你的想法。';$('#article').append(empty);}
  $('#highlight-count').textContent = `已劃記 ${work.highlights.length} 段`;
  selected=[]; updateSelectionButton();
}
function updateSelectionButton() {
  const button=$('#toggle-highlight');
  button.disabled=selected.length === 0 || chatBusy;
  button.textContent = !selected.length ? '先選取對話文字' : isCovered(work.highlights,selected) ? '取消這段劃記' : '劃記選取文字';
}
function captureSelection() {
  if (!work || views.reading.hidden) return;
  const selection=window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) { selected=[]; updateSelectionButton(); return; }
  const range=selection.getRangeAt(0);
  if (!$('#article').contains(range.startContainer) || !$('#article').contains(range.endContainer)) {
    selected=[]; updateSelectionButton(); return;
  }
  selected=[];
  $('#article').querySelectorAll('p').forEach(element => {
    if (!range.intersectsNode(element)) return;
    const prefix=document.createRange(); prefix.selectNodeContents(element);
    const p=Number(element.dataset.paragraph);
    let start=0, end=Array.from(article.paragraphs[p].text).length;
    if (element.contains(range.startContainer)) { prefix.setEnd(range.startContainer,range.startOffset); start=Array.from(prefix.toString()).length; }
    if (element.contains(range.endContainer)) { prefix.selectNodeContents(element); prefix.setEnd(range.endContainer,range.endOffset); end=Array.from(prefix.toString()).length; }
    if (end>start) selected.push({p,start,end});
  });
  updateSelectionButton();
}
document.addEventListener('selectionchange',captureSelection);
$('#toggle-highlight').addEventListener('pointerdown',e => { e.preventDefault(); });
$('#toggle-highlight').addEventListener('click',() => {
  if (!selected.length) return;
  work.highlights=toggleRanges(work.highlights,selected);
  window.getSelection()?.removeAllRanges();
  renderArticle(); changed();
});
document.querySelectorAll('[data-stance]').forEach(button => button.addEventListener('click',async () => {
  document.querySelectorAll('[data-stance]').forEach(b => {b.disabled=true;});
  work.stance=button.dataset.stance; work.stage='reading'; changed();
  try { await save(); show('reading'); }
  catch { /* Keep the selected state in memory so retry is possible. */ }
  finally {document.querySelectorAll('[data-stance]').forEach(b => {b.disabled=false;});}
}));
$('#reading-done').addEventListener('click',async () => {
  $('#reading-done').disabled=true;
  work.stage='summary'; changed();
  try { await save(); show('summary'); }
  catch { /* Save already displays an error; retain the page and highlights. */ }
  finally {$('#reading-done').disabled=false;}
});
$('#back-reading').addEventListener('click',() => {work.stage='reading'; changed(); show('reading');});
function renderSummary() {
  $('#summary-list').replaceChildren(...work.highlights.map(h => {
    const li=document.createElement('li'); li.textContent=excerpt(article.paragraphs[h.p].text,h.start,h.end); return li;
  }));
  $('#empty-summary').hidden=work.highlights.length>0;
  $('#inference').value=work.inference;
}
$('#inference').addEventListener('input',() => {
  $('#inference').setCustomValidity(''); work.inference=$('#inference').value; changed();
});
$('#inference-form').addEventListener('submit',async e => {
  e.preventDefault();
  if (submitting) return;
  if (!work.inference.trim()) {$('#inference').setCustomValidity('請填寫你的推論，不能只有空白。'); $('#inference').reportValidity(); return;}
  submitting=true; $('#submit-work').disabled=true; $('#inference').disabled=true; $('#back-reading').disabled=true;
  $('#submit-work').textContent='提交中…';
  try {
    await save();
    const result=await api(`/api/${group}/submit`,payload());
    work=result.work; error(''); show('complete');
  } catch(e) {error(e.message);}
  finally {submitting=false; $('#submit-work').disabled=false; $('#inference').disabled=false; $('#back-reading').disabled=false; $('#submit-work').textContent='提交';}
});
$('#finish-logout').addEventListener('click',async () => {
  try {await api('/api/logout',{}); location.assign('/');} catch(e) {error(e.message);}
});
window.addEventListener('beforeunload',e => {if(changes!==savedChanges || submitting) {e.preventDefault(); e.returnValue='';}});
async function load() {
  try {
    const data=await api(`/api/${group}/work`); article=data.article; student=data.student;
    work=data.work || {messages:[],stance:null,stage:'stance',highlights:[],inference:'',revision:0,submitted_at:null};
    $('#identity').textContent=`${student.classroom} 班・${student.seat} 號`;
    $('#save-status').textContent=data.work ? '正在確認同步狀態…' : '尚未開始';
    syncArticle(); $('#main').hidden=false; show(work.submitted_at ? 'complete' : work.stage,false);
  } catch(e) {error(e.message);}
  finally {$('#loading').hidden=true;}
}
let chatBusy=false;
function syncArticle() {
  article.paragraphs=(work.messages || []).map(m=>({text:m.text,parts:[{text:m.text,bold:false}]}));
}
function pendingQuestion() {
  const last=work.messages?.at(-1);
  return last?.role === 'user' ? last : null;
}
function updateChatControls() {
  const pending=pendingQuestion();
  $('#question').readOnly=Boolean(pending) || chatBusy;
  if(pending) $('#question').value=pending.text;
  $('#send-message').textContent=pending ? '重試回覆' : '傳送';
  $('#send-message').disabled=chatBusy;
  $('#reading-done').disabled=chatBusy;
  updateSelectionButton();
}
let retryRequest=null;
$('#chat-form').addEventListener('submit',async e=>{
  e.preventDefault();
  if(chatBusy) return;
  const text=$('#question').value.trim();
  if(!text) return;
  const pending=pendingQuestion();
  const requestId=pending?.request_id || (retryRequest?.text===text ? retryRequest.id : crypto.randomUUID());
  retryRequest={id:requestId,text};
  chatBusy=true; submitting=true; updateChatControls(); $('#chat-status').textContent='AI 正在思考…'; error('');
  let draftSaved=false;
  try {
    await save(); draftSaved=true;
    const result=await api(`/api/${group}/message`,{...payload(),request_id:requestId,text});
    work=result.work; syncArticle(); renderArticle(); retryRequest=null;
    $('#question').value=''; $('#save-status').textContent='對話已暫存，等待每 35 秒同步';
    $('#chat-status').textContent='';
  } catch(e) {
    const message=e.message;
    if(draftSaved) {
      try {
        const latest=await api(`/api/${group}/work`);
        if(latest.work) {work=latest.work; syncArticle(); renderArticle();}
      } catch { /* Retain local content while offline. */ }
    }
    $('#chat-status').textContent='尚未確認回覆，請重試。';
    error(message);
  } finally {chatBusy=false;submitting=false;updateChatControls();}
});
let syncChecking = false;
async function checkSync() {
  if (!work || work.submitted_at || syncChecking || submitting) return;
  syncChecking = true;
  const revision = work.revision;
  try {
    const status = await api('/api/' + group + '/sync');
    if (work.submitted_at || submitting || revision !== work.revision) return;
    if (changes !== savedChanges) {
      $('#save-status').textContent = '尚有變更未暫存';
      save().catch(() => {});
    } else if (status.failed) {
      $('#save-status').textContent = 'Google 同步延遲，已暫存並將自動重試';
    } else if (status.pending) {
      $('#save-status').textContent = '已暫存，等待每 35 秒同步';
    } else {
      $('#save-status').textContent = status.saved_at
        ? '已同步 Google：' + new Date(status.saved_at).toLocaleTimeString('zh-TW')
        : '尚未開始';
    }
  } catch {
    $('#save-status').textContent = '無法確認同步狀態，請保持連線';
  } finally { syncChecking = false; }
}
setInterval(checkSync, 5000);
load().then(checkSync);
