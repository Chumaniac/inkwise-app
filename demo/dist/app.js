import {HISTORY_KEY,parseHistory} from './history.js';
import {samples,rules,dimensions,codePointLength,detectLanguage,analyzeInput} from './data.js';
const $=id=>document.getElementById(id);
let draftChanged=false;
let current='zh', report=null, confirmed=false, candidates=[], imageUrl=null, toastTimer=null;
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,4200);}
function setReviewPane(pane, focus=false){document.querySelector('.workspace').dataset.pane=pane;for(const key of ['paper','review'])$('pane-'+key).setAttribute('aria-pressed',String(key===pane));if(focus)$(pane==='paper'?'essay-heading':'report-heading').focus();}
for(const key of ['paper','review'])$('pane-'+key).onclick=()=>setReviewPane(key,true);
function updateCount(){$('char-count').textContent=`${codePointLength($('essay').value).toLocaleString()} / 15,000 字符`;$('language-label').textContent=detectLanguage($('essay').value);}
function activePolicy(){return $('policy').value==='auto'?samples[current].policy:$('policy').value;}
function invalidate(){draftChanged=true;report=null;confirmed=false;$('report-state').textContent='等待重新评阅';$('report').replaceChildren(el('div','原文或规则已更改。请核对文字后重新评阅；原报告不会继续用于确认。','pending'));$('input-error').textContent='';updateCount();}
function loadSample(){draftChanged=false;const sample=samples[current];$('essay-title').value=sample.title;$('essay').value=sample.text;$('policy').value='auto';$('rubric-label').textContent=sample.rubric;$('text-confirmed').checked=true;clearImage();updateCount();makeReport(true);}
function makeReport(preview=false){
  try {
    const text=$('essay').value, stats=analyzeInput(text);
    if(!$('text-confirmed').checked)throw Error('请先核对并确认原文，再开始评阅。');
    const sample=samples[current], matched=text===sample.text && $('essay-title').value===sample.title && activePolicy()===sample.policy;
    confirmed=false;
    report={mode:matched?'synthetic-example':'manual-review',title:$('essay-title').value||'未命名作文',text,stats,policy:activePolicy(),rubric:matched?sample.rubric:'自定义内容 · 教师量规待确认',scores:matched&&sample.scores?[...sample.scores]:null,weights:[...sample.weights],feedback:matched?sample.feedback:[],summary:matched?sample.summary:'文本已准备好，等待教师评定。',approved:false,comment:'',knowledgeRelease:'demo-v0.1'};
    $('input-error').textContent='';$('report-state').textContent=preview?'案例预览':'待教师复核';renderReport();
    if(!preview)setReviewPane('review',true);
    if(!preview)toast(matched?'已载入对应的合成案例报告。':'已完成本机文本检查。未调用模型，请由教师填写评分。');
  } catch(error){$('input-error').textContent=error.message;}
}
function renderReport(){
  const root=$('report');root.replaceChildren();
  const hero=el('div',undefined,'scorehero'),circle=el('div',undefined,'scorecircle');
  circle.append(el('strong',report.scores?String(report.scores.reduce((a,b)=>a+b,0)):'—'),el('small',report.scores?'示例 / 100':'待评定'));
  const summary=el('div');summary.append(el('h3',report.summary),el('p',report.mode==='synthetic-example'?'预置合成案例 · 未校准置信度 · 教师最终决定':`${report.stats.length} 字符 · ${report.stats.paragraphs} 段 · ${report.stats.language}。当前由教师手工评定。`));hero.append(circle,summary);root.append(hero);
  dimensions.forEach((name,i)=>{const row=el('div',undefined,'dimension'),bar=el('div',undefined,'bar'),fill=el('i');
    // A native progress element avoids dynamic CSS and honors the strict CSP.
    const meter=document.createElement('progress');meter.max=report.weights[i];meter.value=report.scores?.[i]||0;meter.setAttribute('aria-label',name);bar.replaceChildren(meter);
    row.append(el('span',name),bar,el('b',report.scores?`${report.scores[i]}/${report.weights[i]}`:'待评'));root.append(row);
  });
  const tabs=el('div',undefined,'report-tabs'),body=el('div');
  ['教学反馈','评分依据','复核记录'].forEach((name,i)=>{const b=el('button',name,`report-tab${i===0?' active':''}`);b.setAttribute('aria-pressed',String(i===0));b.addEventListener('click',()=>{for(const child of tabs.children){child.classList.remove('active');child.setAttribute('aria-pressed','false');}b.classList.add('active');b.setAttribute('aria-pressed','true');renderDetail(i,body);});tabs.append(b);});root.append(tabs,body);renderDetail(0,body);
  const teacher=el('details',undefined,'teacher');teacher.append(el('summary','教师修订评分与评语'));
  dimensions.forEach((name,i)=>{const label=el('label',undefined,'adjust');label.append(el('span',`${name} / ${report.weights[i]}`));const input=document.createElement('input');input.id=`score-${i}`;input.type='number';input.min='0';input.max=String(report.weights[i]);input.step='1';if(report.scores)input.value=String(report.scores[i]);input.addEventListener('input',()=>{confirmed=false;report.approved=false;$('report-state').textContent='评分已修改 · 待确认';});label.append(input);teacher.append(label);});
  const commentLabel=el('label','教师评语（修改示例分数时请说明依据）');commentLabel.htmlFor='teacher-comment';const comment=document.createElement('textarea');comment.id='teacher-comment';comment.value=report.comment||'';comment.maxLength=2000;comment.placeholder='用一条具体建议帮助学生完成下一稿';comment.addEventListener('input',()=>{confirmed=false;report.approved=false;$('report-state').textContent='评语已修改 · 待确认';});teacher.append(commentLabel,comment);root.append(teacher);
  const reuse=el('label',undefined,'check'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.id='allow-reuse';reuse.append(checkbox,el('span','允许将本次合成案例放入当前会话的待审核池（不会更新正式规则）'));root.append(reuse);
  const actions=el('div',undefined,'review-actions'),approve=el('button','确认本次评阅（演示）','button primary'),download=el('button','导出报告','button secondary');approve.id='approve';approve.addEventListener('click',approveReport);download.addEventListener('click',downloadReport);actions.append(approve,download);root.append(actions);
}
function renderDetail(index,root){
  root.replaceChildren();
  if(index===0){
    if(!report.feedback.length){root.append(el('p','自定义正文没有预置评语。可在教师修订区填写评分与建议；这里不会把文本统计当作批改结论。','pending'));return;}
    for(const f of report.feedback){const card=el('article',undefined,'feedback'),head=el('h3');head.append(el('span',f.type==='strength'?'亮点':'下一步',`label ${f.type==='improve'?'improve':''}`),el('span',f.title));card.append(head,el('blockquote',`“${f.quote}”`),el('p',f.body),el('small',`${f.rule} · demo-v0.1 · 合成教学反馈`));
      const original=report.text, start=original.indexOf(f.quote);
      if(f.quote && start>=0 && original.indexOf(f.quote,start+1)<0){
        const jump=el('button','定位原文','button secondary');
        jump.type='button';
        jump.addEventListener('click',()=>{
          const essay=$('essay');
          if(essay.value!==original){toast('原文已变动，请重新评阅后定位。');return;}
          setReviewPane('paper');
          essay.focus();
          essay.setSelectionRange(start,start+f.quote.length);
        });
        card.append(jump);
      }
      root.append(card);
    }
  }else if(index===1){
    for(const rule of rules){const card=el('div',`${rule.id} · ${rule.title}`,'evidence-card');card.append(el('small',rule.text));root.append(card);}root.append(el('p','此处展示本地预置规则。生产方案中的 Neo4j / ChromaDB 检索尚未接入。','hint'));
  }else{
    const notes=['校对：原文经体验者确认；没有执行真实 OCR。','评分：预置案例分数或教师手工评定。','反思：本演示不执行模型辩论；处理流程见方案说明。',report.approved?'教师：已在当前会话确认，未签发学校成绩。':'教师：等待复核。'];
    for(const note of notes)root.append(el('p',note,'evidence-card'));
  }
}
function approveReport(){
  if(!report)return;
  if(confirmed){toast('本次评阅已确认。修改内容后可重新评阅。');return;}
  const values=dimensions.map((_,i)=>$(`score-${i}`).value);
  const scores=values.map(Number);
  if(values.some(v=>v==='')||scores.some((v,i)=>!Number.isInteger(v)||v<0||v>report.weights[i])){toast('请为五个维度填写范围内的整数分数。');return;}
  const comment=$('teacher-comment').value.trim();
  if((!report.scores||scores.some((v,i)=>v!==report.scores[i]))&&!comment){toast('修改或填写评分时，请写明教师判断依据。');return;}
  draftChanged=true;
  report={...report,scores,comment,approved:true,approvedAt:new Date().toISOString()};confirmed=true;$('report-state').textContent='已确认 · 仅当前会话';
  const hero=$('report').querySelector('.scorecircle');hero.querySelector('strong').textContent=scores.reduce((a,b)=>a+b,0);hero.querySelector('small').textContent='确认 / 100';
  $('report').querySelectorAll('.dimension').forEach((row,i)=>{row.querySelector('progress').value=scores[i];row.querySelector('b').textContent=`${scores[i]}/${report.weights[i]}`;});
  if($('allow-reuse').checked){
    if(report.mode!=='synthetic-example'){toast('自定义文字不进入公开演示候选池；报告已在本机会话确认。');return;}
    if(!candidates.some(c=>c.text===report.text&&c.rubric===report.rubric)){candidates.push({...report});if(candidates.length>10)candidates.shift();renderKnowledge();}
    toast('已确认，并加入当前会话待审核池。正式规则保持原版本。');
  }else toast('已在当前会话确认，可导出报告。');
}
function downloadReport(){if(!report)return;const payload={...report,disclaimer:'合成案例或手工评定演示，不是 AI 实测或学校正式成绩。',exportedAt:new Date().toISOString()};const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='inkwise-demo-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function renderKnowledge(){const root=$('rules-list');root.replaceChildren();for(const rule of rules){const card=el('article',undefined,'rule-item');card.append(el('h3',`${rule.id} · ${rule.title}`),el('p',rule.text),el('small','合成量规 / demo-v0.1'));root.append(card);}
  $('candidate-count').textContent=`${candidates.length} 条`;$('candidates').replaceChildren();if(!candidates.length)$('candidates').append(el('div','还没有待审核案例。\n在评阅报告中确认合成案例，并选择允许回流。','empty'));
  candidates.forEach((c,index)=>{const card=el('article',undefined,'rule-item'),remove=el('button','撤回候选','button secondary');card.append(el('h3',c.title),el('p',`教师确认 ${c.scores.reduce((a,b)=>a+b,0)} 分 · 等待教研复核`),el('small','仅保留当前页面会话；刷新后清空。'));remove.addEventListener('click',()=>{candidates.splice(index,1);renderKnowledge();toast('已撤回候选。');});card.append(remove);$('candidates').append(card);});
}
function clearImage(){if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=null;$('image-preview').removeAttribute('src');$('image-preview').hidden=true;$('clear-image').hidden=true;$('image-input').value='';}
$('image-input').addEventListener('change',()=>{const file=$('image-input').files[0];if(!file)return;if(!['image/png','image/jpeg'].includes(file.type)||file.size>10*1024*1024){clearImage();toast('请选择不超过 10 MB 的 PNG 或 JPEG 图片。');return;}if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=URL.createObjectURL(file);$('image-preview').src=imageUrl;$('image-preview').hidden=false;$('clear-image').hidden=false;$('text-confirmed').checked=false;invalidate();toast('图片仅供本机预览。请手工转写并核对原文。');});
$('clear-image').addEventListener('click',clearImage);$('image-preview').addEventListener('error',()=>{clearImage();toast('图片无法解码，请换一张有效图片。');});
$('report').addEventListener('input',()=>{draftChanged=true;});
$('sample').addEventListener('change',()=>{
  if((draftChanged||imageUrl)&&!window.confirm('换用所选案例并放弃当前原文和复核编辑？')){
    $('sample').value=current;
    return;
  }
  current=$('sample').value;
  loadSample();
});$('essay').addEventListener('input',()=>{$('text-confirmed').checked=false;invalidate();});$('essay-title').addEventListener('input',invalidate);$('policy').addEventListener('change',()=>{$('rubric-label').textContent='语言规则已切换 · 需教师确认量规';invalidate();});$('text-confirmed').addEventListener('change',()=>{if(!$('text-confirmed').checked)invalidate();});$('grade').addEventListener('click',()=>makeReport());
$('reset').addEventListener('click',()=>{if((draftChanged||imageUrl||candidates.length)&&!window.confirm('重置当前编辑和本次候选池？已保存历史会保留。'))return;current='zh';$('sample').value='zh';candidates=[];loadSample();renderKnowledge();toast('当前编辑已重置；已保存历史保留，可单独清除。');});
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{const view=button.dataset.view;document.querySelectorAll('.view').forEach(node=>node.hidden=node.id!==`${view}-view`);document.querySelectorAll('[data-view]').forEach(node=>{node.classList.toggle('active',node===button);if(node===button)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');});$('breadcrumb').textContent=button.textContent.replace(/[▤▦◇]/g,'').trim();}));
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'&&!$('work-view').hidden){e.preventDefault();makeReport();}});
window.addEventListener('pagehide',()=>{clearImage();clearTimeout(toastTimer);});loadSample();renderKnowledge();

let demoHistory=[],historyInvalid=false;
try{demoHistory=parseHistory(sessionStorage.getItem(HISTORY_KEY));}catch(error){historyInvalid=true;$('demo-history-state').textContent=error.message;}
function renderDemoHistory(){
 $('demo-history-list').replaceChildren();
 for(const item of demoHistory){const row=el('article',undefined,'history-row'),open=el('button','打开报告','button secondary'),download=el('button','导出 JSON','button secondary');row.append(el('strong',item.report.title),el('span',`${item.report.scores?item.report.scores.reduce((a,b)=>a+b,0)+' 分':'待评定'} · ${item.report.approved?'已确认演示':'待复核'} · ${new Date(item.savedAt).toLocaleString('zh-CN')}`));
 open.addEventListener('click',()=>{if(!window.confirm('载入这份已保存报告？当前未保存编辑将被替换。'))return;const match=Object.entries(samples).find(([,s])=>s.text===item.report.text);draftChanged=false;current=match[0];$('sample').value=current;$('essay-title').value=item.report.title;$('essay').value=item.report.text;$('policy').value=item.report.policy;$('text-confirmed').checked=true;clearImage();updateCount();report=structuredClone(item.report);confirmed=report.approved;renderReport();$('report-state').textContent='已载入历史 · '+(confirmed?'当时已确认':'待教师复核');setReviewPane('review',true);});
 download.addEventListener('click',()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(item,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='inkwise-saved-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});row.append(open,download);$('demo-history-list').append(row);}
 if(!demoHistory.length&&!historyInvalid)$('demo-history-state').textContent='尚未保存合成报告。';
 $('save-demo-report').disabled=historyInvalid;
}
$('save-demo-report').addEventListener('click',()=>{try{
 if(!report||report.mode!=='synthetic-example')throw Error('历史仅保存合成案例。自定义内容请直接导出。');
 const copy=structuredClone(report);if(!confirmed){copy.comment=$('teacher-comment')?.value||'';const values=dimensions.map((_,i)=>$(`score-${i}`).value);if(!values.every((v,i)=>v!==''&&Number.isInteger(Number(v))&&Number(v)>=0&&Number(v)<=copy.weights[i]))throw Error('请为五个维度填写范围内的整数分数，再保存报告。');copy.scores=values.map(Number);}
 const latestHistory=parseHistory(sessionStorage.getItem(HISTORY_KEY));
 const next=[{id:crypto.randomUUID(),savedAt:new Date().toISOString(),report:copy},...latestHistory].slice(0,10),raw=JSON.stringify({version:1,items:next});parseHistory(raw);sessionStorage.setItem(HISTORY_KEY,raw);demoHistory=next;draftChanged=false;renderDemoHistory();$('demo-history-state').textContent='已保存合成报告，关闭标签页前可导出。';
 }catch(error){$('demo-history-state').textContent=error.message;}});
$('clear-demo-history').addEventListener('click',()=>{if(!window.confirm('清除本标签页保存的演示历史？'))return;try{sessionStorage.removeItem(HISTORY_KEY);demoHistory=[];historyInvalid=false;renderDemoHistory();}catch{$('demo-history-state').textContent='清除失败，请检查浏览器存储权限。';}});
renderDemoHistory();

$('guide-case').onclick=()=>{$('sample').focus();$('sample').scrollIntoView({block:'center'});};
$('guide-paper').onclick=()=>{setReviewPane('paper');$('essay').focus();};
$('guide-review').onclick=()=>setReviewPane('review',true);
