import {constraints,datasets} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {status} from '../components/common/status.js';
import {FailureKind} from '../constants/enums.js';

const editable=()=>session.user.role!=='viewer';

function parsePairs(text){
  return text.split(/\n+/).map(line=>line.trim()).filter(Boolean).map(line=>{
    const parts=line.split(/[，,]/).map(v=>v.trim()).filter(Boolean);
    return parts;
  });
}
const serialisePairs=pairs=>(pairs||[]).map(p=>p.join('，')).join('\n');

function issueList(report){
  if(!report)return '';
  if(!report.issues?.length)return '<p class="ok-note">校验通过，可保存为新版本。</p>';
  return '<ul class="issue-list">'+report.issues.map(i=>'<li class="issue-'+esc(i.code)+'"><strong>'+esc(i.code)+'</strong> '+esc(i.message)+'</li>').join('')+'</ul>'+(report.truncated?'<p class="hint">问题较多，仅显示前若干条。</p>':'');
}

function versionsPanel(state){
  const {dataset,versions}=state;
  if(!dataset)return '<p class="empty">暂无数据批次。</p>';
  const active=versions?.versions?.find(v=>v.is_active);
  return '<div class="version-summary"><div><span class="eyebrow">现行版本</span><strong>'+(active?'v'+active.version:'无约束版本')+'</strong><small>'+(active?active.constraint_hash.slice(0,16)+'…':'原有无约束分析仍可用')+'</small></div><div><span class="eyebrow">版本数</span><strong>'+(versions?.versions?.length||0)+'</strong><small>换版不覆盖历史</small></div></div>'+
    '<div class="constraint-lines"><div><h3>同群关系</h3>'+(active?.must_links?.length?'<ul>'+active.must_links.map(p=>'<li>'+esc(p[0])+' × '+esc(p[1])+'</li>').join('')+'</ul>':'<p class="hint">无</p>')+'</div><div><h3>互斥关系</h3>'+(active?.cannot_links?.length?'<ul>'+active.cannot_links.map(p=>'<li>'+esc(p[0])+' ⊥ '+esc(p[1])+'</li>').join('')+'</ul>':'<p class="hint">无</p>')+'</div></div>'+
    '<div class="section-title"><h2>版本历史</h2><span>旧版触发失效，不删记录</span></div><div class="version-list">'+(versions?.versions||[]).map(v=>'<article class="version-row '+(v.is_active?'active':'')+'"><div><strong>v'+v.version+' '+(v.is_active?'· 现行':'')+'</strong><p>'+esc(v.note||'无备注')+'</p><small>'+date(v.created_at)+' · 同群 '+v.must_links.length+' 对 · 互斥 '+v.cannot_links.length+' 对</small></div><code>'+esc(v.constraint_hash.slice(0,18))+'…</code></article>').join('')+(!versions?.versions?.length?'<p class="empty">尚无约束版本，分析将按无约束方式运行。</p>':'')+'</div>';
}

function editorPanel(state){
  const {dataset,jobList}=state;
  if(!dataset)return '';
  const active=state.versions?.versions?.find(v=>v.is_active);
  return '<form id="constraint-form"><div class="editor-head"><h2>编辑并发布新版本</h2><span>每行一对批次号，用逗号分隔</span></div>'+
    '<label>同群关系（必须同群）<textarea name="must" rows="5" placeholder="QH-001，QH-002">'+esc(serialisePairs(active?.must_links))+'</textarea></label>'+
    '<label>互斥关系（不得同群）<textarea name="cannot" rows="5" placeholder="QH-001，QH-121">'+esc(serialisePairs(active?.cannot_links))+'</textarea></label>'+
    '<label>版本备注<input name="note" maxlength="500" placeholder="例如：10 月评审会确认 QH-001/002 必须同群"></label>'+
    '<input type="hidden" name="base_version_id" value="'+(active?.id??'')+'">'+
    '<div class="editor-actions"><button type="button" id="validate-constraint" class="button">校验约束</button><button type="submit" class="primary" '+(!editable()?'disabled':'')+'>发布新版本</button></div>'+
    '<p class="hint">发布即成为唯一现行版本；绑定旧版的分析会被标记失效，已批准的自动撤回，相关简报自动暂停。同一幂等键重试不会新增版本。</p><div id="constraint-report" class="validation-report"></div></form>';
}

function jobsEvidence(state){
  const {jobList}=state;
  if(!jobList?.length)return '<p class="empty">该批次还没有分析任务。</p>';
  return jobList.map(j=>{
    const r=j.result||{}, c=r.constraint, failure=r.failure;
    const badge=j.invalidated_at?'<span class="status invalidated">已失效</span>':status(j.status);
    let body='';
    if(j.invalidated_at){
      body='<p class="invalidation-reason"><strong>失效原因：</strong>'+esc(j.invalidation_reason||'约束版本已更新')+'</p>';
    }
    if(failure){
      body+='<div class="failure failure-'+esc(failure.kind)+'"><strong>'+esc(FailureKind[failure.kind]||failure.kind)+'：</strong>'+esc(failure.message)+(failure.evidence?.length?'<ul>'+failure.evidence.map(e=>'<li>'+esc(e)+'</li>').join('')+'</ul>':'')+'<small>搜索节点 '+(failure.search?.nodes??'—')+' · 用时 '+(failure.search?.elapsed_ms??'—')+' ms</small></div>';
    }
    if(c){
      const v=c.verification||{};
      body+='<div class="evidence"><p>绑定约束 v'+esc(c.version)+' · 同群 '+v.must_links_satisfied+'/'+v.must_links_total+' · 互斥 '+v.cannot_links_satisfied+'/'+v.cannot_links_total+' · 非空群 '+v.non_empty_clusters+'/'+v.clusters+'</p>'+
        '<div class="component-table">'+(c.components||[]).map(comp=>'<details><summary>'+esc(comp.group)+' → '+esc(comp.cluster)+'（'+comp.size+' 批）</summary><p class="members">'+comp.members.map(m=>'<span>'+esc(m)+'</span>').join('')+'</p>'+(comp.must_edges?.length?'<small>同群边：'+comp.must_edges.map(e=>esc(e[0])+'×'+esc(e[1])).join('，')+'</small>':'')+'</details>').join('')+'</div>'+
        '<p class="hint">簇内平方和 '+(c.objective?.constrained_inertia??'—')+'（无约束 '+(c.objective?.unconstrained_inertia??'—')+'）；标签按中心温度排序，不存在优劣。</p></div>';
    }
    return '<article class="evidence-card"><div class="evidence-head"><div><strong>'+esc(j.name)+'</strong><small>'+date(j.created_at)+' · 绑定 '+(j.constraint_version?('v'+j.constraint_version):'无约束')+'</small></div>'+badge+'</div>'+body+'</article>';
  }).join('');
}

export async function constraintPage(datasetId){
  workspace.datasets=await datasets.list();
  const wanted=Number(datasetId);
  const dataset=workspace.datasets.find(d=>d.id===wanted)||workspace.datasets[0];
  const state={dataset,versions:null,jobList:null};
  if(dataset){
    [state.versions,state.jobList]=await Promise.all([constraints.list(dataset.id),constraints.jobs(dataset.id)]);
  }
  window.__constraintState=state;
  return heading('03 / RULES','工艺评审约束','按批次号维护同群与互斥关系。每次发布形成新版本；旧版分析立即失效，缺号、矛盾与无解都给出可核对的依据。','<button class="button" id="refresh-constraints">刷新 ↻</button>')+
    (dataset?'<div class="constraint-grid"><section class="panel">'+
      '<label>数据批次<select id="constraint-dataset">'+workspace.datasets.map(d=>'<option value="'+d.id+'" '+(d.id===dataset.id?'selected':'')+'>'+esc(d.name)+' · '+d.rows+' 条</option>').join('')+'</select></label>'+
      versionsPanel(state)+'</section><section class="panel">'+editorPanel(state)+'</section></div>'+
      '<section class="panel"><div class="section-title"><h2>归群依据与失效记录</h2><span>'+state.jobList.length+' 个任务</span></div><div id="evidence-list">'+jobsEvidence(state)+'</div></section>'
      :'<p class="empty">先导入数据批次。</p>');
}

export function bindConstraints(refresh){
  const state=window.__constraintState;
  document.querySelector('#refresh-constraints')?.addEventListener('click',refresh);
  document.querySelector('#constraint-dataset')?.addEventListener('change',e=>{location.hash='#/constraints/'+e.target.value;refresh();});
  const form=document.querySelector('#constraint-form');
  if(!form)return;
  const read=()=>({must_links:parsePairs(form.querySelector('[name=must]').value),cannot_links:parsePairs(form.querySelector('[name=cannot]').value)});
  const report=form.querySelector('#constraint-report');
  const showReport=r=>{report.innerHTML=issueList(r);};
  form.querySelector('#validate-constraint').onclick=async()=>{
    try{showReport(await constraints.validate(state.dataset.id,read()));}catch(e){notify(e.message,true);}
  };
  submit(form,async f=>{
    const payload=read();
    const key=(crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random().toString(16).slice(2));
    const base=f.get('base_version_id');
    try{
      const v=await constraints.create(state.dataset.id,{...payload,note:f.get('note')||'',base_version_id:base?Number(base):null},key);
      notify(v.replayed?'内容未变化，沿用现行 v'+v.version:'已发布约束 v'+v.version);
      await refresh();
    }catch(e){notify(e.message,true);}
  });
}

