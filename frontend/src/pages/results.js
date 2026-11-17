import {jobs} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {chart} from '../components/common/chart.js';
import {table} from '../components/common/table.js';
import {status} from '../components/common/status.js';
import {FailureKind} from '../constants/enums.js';
import {esc,fmt,date,notify} from '../utils/dom.js';

function constraintBlock(j){
  const r=j.result||{},c=r.constraint;
  if(!c)return '';
  const v=c.verification||{},o=c.objective||{};
  return '<section class="panel constraint-evidence"><div class="section-title"><h2>约束满足依据</h2><span>绑定 v'+esc(c.version)+' · hash '+esc(String(c.constraint_hash).slice(0,12))+'…</span></div>'+
    '<div class="verify-grid"><div><span>同群关系</span><strong>'+v.must_links_satisfied+'/'+v.must_links_total+'</strong></div><div><span>互斥关系</span><strong>'+v.cannot_links_satisfied+'/'+v.cannot_links_total+'</strong></div><div><span>非空群</span><strong>'+v.non_empty_clusters+'/'+v.clusters+'</strong></div><div><span>簇内平方和</span><strong>'+esc(fmt(o.constrained_inertia))+'<small>无约束 '+esc(fmt(o.unconstrained_inertia??'—'))+'</small></strong></div></div>'+
    '<div class="component-table">'+(c.components||[]).map(comp=>'<details'+(comp.must_edges?.length?' open':'')+'><summary>'+esc(comp.group)+' → '+esc(comp.cluster)+'（'+comp.size+' 批'+(comp.must_edges?.length?' · 同群 '+comp.must_edges.length+' 对':'')+'）</summary><p class="members">'+comp.members.map(m=>'<span>'+esc(m)+'</span>').join('')+'</p></details>').join('')+'</div>'+
    '<p class="hint">搜索节点 '+esc(c.search?.nodes??'—')+' · 用时 '+esc(c.search?.elapsed_ms??'—')+' ms；标签按中心温度排序，不存在优劣。</p></section>';
}

function failureBlock(j){
  const f=(j.result||{}).failure;
  if(!f)return '';
  return '<section class="panel failure failure-'+esc(f.kind)+'"><div class="section-title"><h2>'+esc(FailureKind[f.kind]||f.kind)+'</h2><span>'+(f.kind==='timeout'?'搜索预算耗尽，不能据此断言无解':'已给出判定依据')+'</span></div><p>'+esc(f.message)+'</p>'+(f.evidence?.length?'<ul class="questions">'+f.evidence.map(e=>'<li>'+esc(e)+'</li>').join('')+'</ul>':'')+(f.search?'<p class="hint">搜索节点 '+esc(f.search.nodes??'—')+' · 用时 '+esc(f.search.elapsed_ms??'—')+' ms'+(f.search.proven?' · 已穷举':'')+'</p>':'')+'<a href="#/workbench">调整约束或 K 后重算 ↗</a></section>';
}

export async function results(id){
 workspace.jobs=await jobs.list();const j=id?await jobs.get(id):(workspace.jobs.find(x=>x.id===workspace.selectedJob)||workspace.jobs[0]);
 if(j&&!workspace.jobs.some(x=>x.id===j.id))workspace.jobs.push(j);
 if(!j)return heading('05 / REVIEW','结果复核','尚无分析结果。')+'<div class="empty">先到分析工作台，选择批次并提交一次计算。<a href="#/workbench">前往工作台 ↗</a></div>';
 workspace.selectedJob=j.id;const r=j.result||{};
 const versionLabel=j.constraint_version?('约束 v'+j.constraint_version):'无约束';
 const invalid=j.status==='invalidated'||j.invalidated_at;
 return heading('05 / REVIEW','结果复核与业务解释',j.name,'<button class="button" id="refresh-results">刷新结果 ↻</button>')+'<div class="result-selector"><label>分析记录<select id="select-job">'+workspace.jobs.map(x=>'<option value="'+x.id+'" '+(x.id===j.id?'selected':'')+'>'+esc(x.name)+' · '+esc(x.status)+'</option>').join('')+'</select></label>'+status(j.status)+'<span>'+esc(versionLabel)+'</span><span>'+date(j.created_at)+'</span><span class="approval">'+(invalid?'已失效':(j.approved?'已获业务批准':'待负责人复核'))+'</span></div>'+
  (invalid?'<section class="panel failure failure-invalidated"><div class="section-title"><h2>结果已失效</h2><span>拒收旧版结果</span></div><p>'+esc(j.invalidation_reason||'约束规则已更新，该分析绑定旧版规则。')+'</p><p class="hint">旧结果不再可批准，也不会进入分发简报。请按现行约束重新发起分析。</p><a href="#/workbench">按现行约束重算 ↗</a></section>':'')+
  failureBlock(j)+
  (j.status==='completed'?'<section class="result-lead"><p class="eyebrow">COMPUTED FINDING</p><h2>'+esc(r.summary)+'</h2><div class="metrics">'+(r.metrics||[]).map(m=>'<div><span>'+esc(m.label)+'</span><strong>'+esc(fmt(m.value))+'<small>'+esc(m.unit||'')+'</small></strong></div>').join('')+'</div></section><section class="panel">'+chart(r.chart)+'</section><div class="two-columns"><section class="panel"><div class="section-title"><h2>业务解释</h2><span>由计算结果生成</span></div><ol class="questions">'+(r.findings||[]).map(v=>'<li>'+esc(v)+'</li>').join('')+'</ol>'+(r.warnings||[]).map(v=>'<p class="warning">'+esc(v)+'</p>').join('')+'</section><section class="method-sheet compact"><p class="eyebrow">REPRODUCIBILITY</p><h3>计算口径与参数</h3><p>'+esc(r.method)+'</p><pre>'+esc(JSON.stringify({params:j.params,constraint_version_id:j.constraint_version_id,constraint_version:j.constraint_version,constraint_hash:j.constraint_hash},null,2))+'</pre>'+(session.user.role==='admin'&&!j.approved&&!invalid?'<button class="primary" id="approve-job">批准此业务结论</button>':'')+'<p class="hint">仅批准结果可进入定时分发档案；换版后旧批准自动撤回。</p></section></div>'+constraintBlock(j)+'<section class="panel"><div class="section-title"><h2>结果明细</h2><span>'+(r.table?.rows?.length||0)+' 行</span></div>'+table(r.table?.columns||[],r.table?.rows||[])+'</section>':'<section class="panel empty">'+(j.status==='failed'?'计算失败：'+esc(j.error)+'。请返回工作台调整参数或数据。':(invalid?'该结果已被约束换版拒收。':'后台正在执行计算。点击刷新结果查看最新进展。'))+'<a href="#/workbench">返回工作台 ↗</a></section>');
}
export function bindResults(refresh){document.querySelector('#refresh-results')?.addEventListener('click',refresh);document.querySelector('#select-job')?.addEventListener('change',e=>{workspace.selectedJob=Number(e.target.value);location.hash='#/results/'+e.target.value;});document.querySelector('#approve-job')?.addEventListener('click',async()=>{try{await jobs.approve(workspace.selectedJob);notify('已批准，可创建分发计划');await refresh();}catch(e){notify(e.message,true);}});}
