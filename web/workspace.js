const $=id=>document.getElementById(id);
let citation=null,citationRequest=0,currentImageHash=null;
function clearCitation(){citation=null;citationRequest++;$('citation-location').hidden=true;$('citation-excerpt').replaceChildren();$('citation-status').textContent='';$('citation-boxes').replaceChildren();}
function drawCitationBoxes(){
  const root=$('citation-boxes');root.replaceChildren();if(!citation||!job||citation.jobId!==job.id||citation.jobVersion!==job.version||$('confirmed-text').value!==job.text)return;
  const selected=selectedImage(),page=citation.pages.find(page=>page.page===selected.page),image=$('original-image');
  if(selected.archived||!page||!currentImageHash||page.imageSha256!==currentImageHash||!image.naturalWidth){if(citation.status==='region')$('citation-status').textContent='当前页没有可核对的引用区域，或原图摘要不一致。';return;}
  const width=image.clientWidth,height=image.clientHeight,scale=Math.min(width/image.naturalWidth,height/image.naturalHeight),w=image.naturalWidth*scale,h=image.naturalHeight*scale;
  for(const box of page.boxes){const region=node('span',undefined,'citation-box');region.style.left=((width-w)/2+box[0]*w)+'px';region.style.top=((height-h)/2+box[1]*h)+'px';region.style.width=(box[2]*w)+'px';region.style.height=(box[3]*h)+'px';root.append(region);}
  if(page.boxes.length)$('citation-status').textContent=`引用已定位到确认文字与原稿第 ${page.page} 页（OCR 行区域，请对照核对）。`;
}
async function locateCitation(evidence){
  setJobStage('source',false);clearCitation();$('citation-location').hidden=false;
  if($('confirmed-text').value!==job.text){$('citation-status').textContent='文字有未保存修改，请先核对当前文字；未套用旧引用位置。';return;}
  const id=job.id,version=job.version,selection=selectionVersion,request=++citationRequest;
  const data=await api(`/jobs/${id}/citation-location`,{method:'POST',body:{version,textVersion:job.textVersion,textHash:job.textHash,quote:evidence.quote,start:evidence.start,end:evidence.end}});
  if(request!==citationRequest||selection!==selectionVersion||job?.id!==id||job.version!==version||$('confirmed-text').value!==job.text)return;
  citation={...data,jobId:id};
  const area=$('confirmed-text');if(jobStage==='source'){area.focus();area.setSelectionRange(data.startUtf16,data.endUtf16);area.scrollIntoView({block:'center'});}
  const preview=$('citation-excerpt'),mark=node('mark',job.text.slice(data.startUtf16,data.endUtf16));preview.replaceChildren(node('span',job.text.slice(Math.max(0,data.startUtf16-80),data.startUtf16)),mark,node('span',job.text.slice(data.endUtf16,data.endUtf16+80)));
  const reasons={'report-binding-unrecorded':'原报告未记录文字绑定，仅定位确认文字。','repeated-quote':'引用出现多次，仅定位报告指定的文字范围；原稿区域需人工核对。','transcript-edited':'确认文字与 OCR 转写不同，仅定位文字；原稿区域需重新核对。','line-order-uncertain':'OCR 行顺序无法可靠对应，仅定位文字。','coordinates-unavailable':'没有可靠坐标，仅定位文字或整页。','region-limit':'引用区域超过显示上限，仅定位文字。'};
  $('citation-status').textContent=reasons[data.reason]||'正在核对原稿摘要…';
  if(data.pages.length&&job.hasImage){$('image-page').value=String(data.pages[0].page);await loadImage();if(request===citationRequest)drawCitationBoxes();}
}
function citationButton(evidence){const button=node('button','定位原文','citation-action');button.type='button';button.addEventListener('click',()=>busy(button,()=>locateCitation(evidence)));return button;}
window.addEventListener('resize',drawCitationBoxes);

let handoff=null,handoffIdentity='',handoffAborter=null,handoffRequest=0;
let reportReadiness=null,readinessKey='',readinessRequest=0,readinessAborter=null;
function clearHandoff(){handoffRequest++;handoffAborter?.abort();handoffAborter=null;handoff=null;handoffIdentity='';$('handoff-preview').hidden=true;$('handoff-content').textContent='';$('handoff-status').textContent='';}
function currentHandoffIdentity(){return job?`${selectionVersion}:${job.id}:${job.version}`:'';}
async function generateHandoff(){
  if(!job)return;
  if(dirty){notify('有尚未保存的修改，请先保存或确认后再生成交接摘要。');return;}
  clearHandoff();const request=handoffRequest,identity=currentHandoffIdentity();handoffIdentity=identity;handoffAborter=new AbortController();
  const data=await api(`/jobs/${job.id}/handoff?version=${job.version}`,{signal:handoffAborter.signal});
  if(request!==handoffRequest||identity!==currentHandoffIdentity()||dirty||!token)return;
  const keys=['schema','jobId','tenantId','classroomId','jobVersion','textVersion','status','rubricRelease','modelRevision','pipelineRevision','errorCode','lastEvent','nextAction','instruction','updatedAt','generatedAt'];
  if(!data||typeof data!=='object'||Object.keys(data).length!==keys.length||!keys.every(key=>Object.hasOwn(data,key))||data.schema!=='inkwise.task-handoff/v1'||data.jobId!==job.id||data.jobVersion!==job.version||JSON.stringify(data).length>8192||
    !['tenantId','classroomId'].every(key=>typeof data[key]==='string'&&data[key].length<=80)||!Number.isSafeInteger(data.textVersion)||data.textVersion<0||
    !['rubricRelease','modelRevision','pipelineRevision','errorCode'].every(key=>data[key]===null||(typeof data[key]==='string'&&data[key].length<=256&&/^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/.test(data[key])))||
    !['confirm_text','review_report','inspect_failure','read_report','closed','wait','inspect_events'].includes(data.nextAction)||!(Object.hasOwn(states,data.status)||data.status==='unknown')||typeof data.instruction!=='string'||data.instruction.length>256||
    !['updatedAt','generatedAt'].every(key=>typeof data[key]==='string'&&data[key].length<=40&&Number.isFinite(Date.parse(data[key])))||
    (data.lastEvent!==null&&(!data.lastEvent||Object.keys(data.lastEvent).sort().join(',')!=='createdAt,event,id,version'||!Number.isSafeInteger(data.lastEvent.id)||!Number.isSafeInteger(data.lastEvent.version)||
      (data.lastEvent.event!==null&&(typeof data.lastEvent.event!=='string'||data.lastEvent.event.length>64))||typeof data.lastEvent.createdAt!=='string'||data.lastEvent.createdAt.length>40))){clearHandoff();throw Error('交接摘要格式不完整，请重新读取当前版本。');}
  handoff=data;$('handoff-content').textContent=JSON.stringify(data,null,2);$('handoff-preview').hidden=false;$('handoff-status').textContent=`任务版本 ${data.jobVersion}，可下载交接摘要。接手者需有该班级访问权限。`;
}
$('generate-handoff').addEventListener('click',()=>busy($('generate-handoff'),generateHandoff));
$('download-handoff').addEventListener('click',()=>{
  if(!handoff||handoffIdentity!==currentHandoffIdentity()||dirty||!token){clearHandoff();notify('任务或未保存内容已变化，请重新生成交接摘要。');return;}
  const url=URL.createObjectURL(new Blob([JSON.stringify(handoff,null,2)],{type:'application/json'})),link=node('a');link.href=url;link.download=`inkwise-handoff-${job.id.replace(/[^a-zA-Z0-9-]/g,'')}-v${job.version}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
const labels={content:'内容与任务',coherence:'结构与连贯',expression:'表达与细节',vocabulary:'词汇运用',grammar:'语法与规范'};
const proposalStates={'proposal-not-active':'改进建议（未启用）',submitted:'待独立审核','approved-inactive':'已审核，等待对照结果',published:'已启用'};
const learningReasons={PINNED_MODEL_AND_PIPELINE_REQUIRED:'需要可追溯的模型评阅记录',BASELINE_NOT_CURRENT:'案例量规与当前版本不一致',MORE_APPROVED_CASES_REQUIRED:'已审核案例数量不足',STUDENT_GROUP_KEYS_REQUIRED:'案例缺少学习者代号',INVALID_SOURCE_SCORES:'部分案例缺少完整评分，请先核对',WEIGHTS_FIXED_BY_POLICY:'当前量规不允许自动调整权重',INDEPENDENT_HOLDOUT_REQUIRED:'可用于独立评估的学习者不足',CUMULATIVE_WEIGHT_LIMIT:'累计调整已达到允许范围',HELD_OUT_MAE_REGRESSED:'新方案误差增大，暂不建议采用',ORDINAL_AGREEMENT_BELOW_POLICY:'与教师评分的一致性尚未达到要求',ORDINAL_AGREEMENT_REGRESSED:'与教师评分的一致性下降',NO_MATERIAL_WEIGHT_CHANGE:'现有结果不足以支持改变权重',NO_NEW_APPROVED_FEEDBACK:'暂无新增的已审核反馈'};
const candidateStates={pending:'待审核',approved:'已审核',rejected:'已退回',published:'已入库',withdrawn:'已撤回'};
const states={needs_text_review:'待核对原文',ocr_queued:'等待识别',ocr_running:'正在识别',queued:'等待评分',running:'正在评阅',teacher_review:'待教师复核',approved:'教师已确认',published:'已发布',failed:'处理未完成',cancelled:'已取消'};
const queueActions={confirm_text:'下一步：核对原稿与确认文字',review_report:'下一步：核对报告依据并完成教师复核',inspect_failure:'下一步：查看失败原因，未知请求先核查'};
function reassessmentSource(current=job){
  const value=current?.payload?.revisionAssessment;
  const id=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(value);
  if(current?.status==='erased'||!value||typeof value!=='object'||Array.isArray(value)||!id(value.sourceJobId)||!id(value.revisionId)||value.sourceJobId===current.id||!Number.isSafeInteger(value.revisionVersion)||value.revisionVersion<1)return null;
  return {sourceJobId:value.sourceJobId,revisionId:value.revisionId,revisionVersion:value.revisionVersion};
}
function renderReassessmentSource(){ $('reassessment-source').hidden=!reassessmentSource(); }
async function returnToSourceRevision(){
  if(!token||$('workspace').hidden||$('job-panel').hidden)return false;
  const source=reassessmentSource(),childId=job?.id,session=sessionGeneration;
  if(!source||!await selectJob(source.sourceJobId)||session!==sessionGeneration)return false;
  const selection=selectionVersion,intent=selectionRequest;
  await openRevisionWorkspace();
  if(intent!==selectionRequest||selection!==selectionVersion||session!==sessionGeneration||job?.id!==source.sourceJobId)return false;
  await openWritingRevision(source.revisionId);
  if(intent!==selectionRequest||selection!==selectionVersion||session!==sessionGeneration||job?.id!==source.sourceJobId)return false;
  if(writingRevision?.version!==source.revisionVersion||revisionAssessment?.job?.id!==childId){clearReassessment();throw Error('复评关联未确认，请核对首稿的修订记录。');}
  const heading=$('reassessment-heading');heading.tabIndex=-1;heading.focus({preventScroll:true});heading.scrollIntoView({block:'start',behavior:'auto'});
  return true;
}
$('return-revision').addEventListener('click',e=>busy(e.target,returnToSourceRevision));

function nextJobStep(current,role){
  if(!current||!role)return null;
  if(['approved','published'].includes(current.status))return current.result?
    {title:'查看已确认的报告',detail:'本次教师复核已保存，可查看反馈并下载当前版本的报告。',label:'查看报告',target:'report-content'}:
    {title:'查看任务记录',detail:'当前记录没有可显示的报告，请先核对处理记录。',label:'查看处理记录',target:'events'};
  if(!['needs_text_review','teacher_review','failed'].includes(current.status))return null;
  if(!['teacher','admin'].includes(role))return {title:'等待任课教师处理',detail:'当前身份可查看处理记录，请联系任课教师继续核对。',label:'查看处理记录',target:'events'};
  if(current.status==='needs_text_review')return {title:'先核对原文',detail:'对照原稿检查文字和页码顺序，确认后才会提交本次评阅。',label:'前往核对原文',target:'text-form'};
  if(current.status==='failed')return {title:'先查看未完成的原因',detail:'核对失败说明和处理记录。结果未知的请求需要管理员核查。',label:'查看原因',target:current.errorCode?'job-error':'events'};
  if(!current.result)return {title:'报告暂不可用',detail:'先查看处理记录，保留现有任务再核查。',label:'查看处理记录',target:'events'};
  return current.result.kind==='feedback'?
    {title:'复核校对建议',detail:'核对每条建议与对应原文，再确认要保留的反馈。',label:'前往校对复核',target:'feedback-form'}:
    {title:'核对评分和原文依据',detail:'检查各维度得分、原文引用与建议，再填写教师评语。',label:'前往教师复核',target:'review-form'};
}
// Views reuse existing form nodes; navigating within a job never discards edits.
let jobStage='source';
function revisionsAvailable(){return !!(['approved','published'].includes(job?.status)&&job?.review?.scores&&job?.result?.kind!=='feedback');}
function syncJobStage(){
 for(const [stage,panel]of [['source','stage-source-panel'],['report','stage-report-panel'],['revisions','revision-panel'],['records','stage-records-panel']]){
  $(panel).hidden=jobStage!==stage||(stage==='revisions'&&!revisionsAvailable());
  $('stage-'+stage).setAttribute('aria-pressed',String(jobStage===stage));
 }
}
function setJobStage(stage,focus=true){
 if(!job||!['source','report','revisions','records'].includes(stage)||stage==='revisions'&&!revisionsAvailable())return false;
 jobStage=stage;syncJobStage();if(stage==='source')drawCitationBoxes();
 if(focus){const target=$({source:'source-heading',report:'report-heading',revisions:'revision-heading',records:'records-heading'}[stage]);target.focus({preventScroll:true});target.scrollIntoView({block:'start',behavior:'auto'});}
 return true;
}
function focusJobTarget(id){
 const target=$(id);if(!target||target.hidden||target.inert)return;
 const stage=['text-form','confirmed-text','citation-status'].includes(id)?'source':['report-content','review-form','feedback-form','report-readiness'].includes(id)?'report':id==='revision-heading'?'revisions':id==='job-error'?jobStage:'records';
 if(!setJobStage(stage,false))return;target.focus({preventScroll:true});target.scrollIntoView({block:'start',behavior:'auto'});
}
for(const stage of ['source','report','records'])$('stage-'+stage).addEventListener('click',()=>setJobStage(stage));
$('stage-revisions').addEventListener('click',e=>busy(e.target,openRevisionWorkspace));
function renderNextAction(){
  const next=nextJobStep(job,me?.role),root=$('job-next');root.hidden=!next;
  for(const [id,text] of [['job-next-title',next?.title||''],['job-next-detail',next?.detail||''],['job-next-action',next?.label||'']])if($(id).textContent!==text)$(id).textContent=text;
}
$('job-next-action').addEventListener('click',()=>{
  if($('workspace').hidden||$('job-panel').hidden)return;
  const next=nextJobStep(job,me?.role);if(!next)return;
  focusJobTarget(next.target);
});
const errors={MODEL_OUTPUT_INCOMPLETE:'模型输出未完整结束，本次未生成成绩。成功步骤已保存，可恢复一次；再次失败时请检查模型与输出预算。',FEEDBACK_REWRITE_REQUIRED:'选中的评语把原词改成了相同内容，请重写该维度的依据或建议后再确认。',STORAGE_CAPACITY_EXCEEDED:'本机存储余量不足，原作业和原稿已保留；请联系管理员核对空间后再上传。',IMAGE_STORAGE_CAPACITY_EXCEEDED:'原稿存储预算已满，旧稿已保留；请联系管理员调整预算或按已批准的流程清理。',IMAGE_STORAGE_INVALID:'原稿目录暂时无法安全读取，请联系管理员检查；已有原稿未删除。',IMAGE_STORAGE_WRITE_FAILED:'原稿保存回应未确认，请先核对当前作业与本机存储，再决定是否重新上传。',OCR_REPLACEMENT_LIMIT:'本份作业的原稿替换次数已满，旧稿已保留；请新建作业继续。',REPORT_EXPORT_NOT_READY:'报告尚未就绪，请先查看报告就绪检查和下一步。',CITATION_NOT_IN_REPORT:'该引用不在当前报告中，请刷新报告核对。',CITATION_TEXT_CHANGED:'引用与当前文字不一致，请重新核对。',CACHE_CAPACITY_EXCEEDED:'当前任务保留的缓存已达到容量上限，请联系管理员核对，原有结果仍可读取。',STORAGE_CAPACITY_EXCEEDED:'存储余量不足，已停止新增处理，请联系管理员腾出空间后重试。',CACHE_STORAGE_INVALID:'保留结果校验未通过，已停止处理，请联系管理员核对。',MODEL_REPAIR_LIMIT_REACHED:'本次自动修复次数已用完，请联系管理员核对保留的结果。',LEGACY_ROUTE_RECONCILIATION_REQUIRED:'这份历史任务已有模型调用记录，请联系管理员核对后再恢复。',EVALUATION_SALT_REQUIRED:'改进评估尚未完成学校配置，请联系管理员。',EXPERIMENT_EVIDENCE_REQUIRED:'对照证据尚未满足启用要求，请先查看对照评估结果。',OCR_REPLACEMENT_LIMIT:'已达到保留原稿的替换次数上限，请手工核对原文或联系管理员。',IMAGE_UNAVAILABLE:'原稿暂不可用，请联系管理员核对已保存文件。',CANDIDATE_ALREADY_EXISTS:'这份报告已有候选，新内容尚未保存。请到候选知识中修改待审核或被退回的案例。',CANDIDATE_WITHDRAWN:'该版本候选已撤回，不能直接重复提交。',LANGUAGE_POLICY_REQUIRED:'正式量规缺少已审核语言规则，请联系教研负责人。',APPROVED_LANGUAGE_POLICY_REQUIRED:'语言规则尚未审核。',LANGUAGE_REQUIRES_TEACHER:'无法确定可评分语言，请核对原文和量规。',OCR_CACHED_CONFIGURATION_CHANGED:'识别配置已变化，原有成功转写已保留；请联系管理员恢复对应配置。',CLOUD_PROCESSING_NOT_AUTHORIZED:'本次任务没有完整云端处理授权，已停止调用。',CLOUD_COST_BOUND_EXCEEDED:'配置的最大调用费用超过本次预算，请联系管理员核对。',NO_AUTHORIZED_MODEL_ROUTE:'没有满足授权、预算和上下文限制的可用模型。',MODEL_ROUTE_CONFIGURATION_CHANGED:'本次任务的模型配置已变化，请由管理员核对已发出的请求。',MODEL_CONTEXT_BUDGET_EXCEEDED:'当前模型上下文不足，请联系管理员调整配置。',INTENT_UNCLEAR:'处理要求不够明确，请选择评分或仅校对。',EXPLANATION_REQUIRES_REPORT:'请先打开已有报告，再选择解释报告。',OCR_PAGE_LIMIT:'原稿页数超过上限，请拆分作业。',DUPLICATE_PAGE:'检测到重复原稿，请移除重复页后上传。',PAGE_ORDER_INVALID:'页码清单已变化，请刷新后重排。',PAGE_ORDER_CONFIRMATION_REQUIRED:'请核对并确认原稿页码顺序。',MISSING_PAGE_DETECTED:'正文页码提示有缺页，请补齐后重新核对。',OCR_LINE_LIMIT:'页面行数过多，请分成更小的页面或手工核对。',UNSUPPORTED_IMAGE:'请使用 PNG 或 JPEG 图片。',IMAGE_DIMENSIONS_INVALID:'图片尺寸无效或像素过多，请缩小后上传。',NORMALIZED_IMAGE_TOO_LARGE:'处理后的图片仍然过大，请降低分辨率。',OCR_TIMEOUT:'识别超时，请稍后重试或手工核对原文。',IMAGE_PROCESSOR_BUSY:'图片正在处理中，请稍后重试。',UNAUTHENTICATED:'登录凭据无效或已撤销。',FORBIDDEN:'当前身份没有此操作权限。',NOT_FOUND:'作业不存在或不属于你的任教班级。',VERSION_CONFLICT:'内容已更新，请刷新后再操作。',VERSION_OR_STATE_CONFLICT:'任务状态已变化，请刷新后再操作。',RULESET_UNAVAILABLE:'没有可用的已发布量规，请联系教研负责人。',RULESET_SCOPE_MISMATCH:'量规不适用于这个年级、体裁或语言政策。',QUEUE_FULL:'当前学校待处理任务已满，请先完成或取消已有任务。',MODEL_NOT_CONFIGURED:'尚未配置评分模型，任务保留等待处理。',PROVIDER_NOT_CONFIGURED:'尚未配置模型服务地址。',PROVIDER_TIMEOUT_UNKNOWN:'模型请求超时，处理结果未知；不会自动重复调用。',PROVIDER_RESULT_UNKNOWN_REQUIRES_REVIEW:'存在结果未知的模型请求，管理员核查前不会重复调用。',OCR_NOT_CONFIGURED:'尚未配置 OCR；可手工转写并确认原文。',OCR_NO_TEXT:'没有识别到有效文字，请检查图片或手工转写。',OCR_FAILED:'本次识别未完成，请检查原稿。',EVIDENCE_QUOTE_INVALID:'模型引用无法对应原文，结果已拦截。',MODEL_INVALID_SCHEMA:'模型返回格式不合要求，结果已拦截。',WORKER_INTERRUPTED:'执行进程中断，成功结果已保留。',BUDGET_EXCEEDED:'本次任务已达到模型调用上限。',INDEPENDENT_REVIEWER_REQUIRED:'需要另一位教研审核者处理此候选。',CANDIDATE_STATE_CONFLICT:'候选状态已改变，请刷新。',IMAGE_ALREADY_ATTACHED:'此任务已保存原稿，请新建任务上传其他原稿。',INVALID_REQUEST:'请检查表单内容与长度。',IMAGE_TOO_LARGE:'图片超过 10 MB。',INVALID_IMAGE:'图片无法识别，请使用有效 PNG 或 JPEG。',RATE_LIMITED:'操作较频繁，请稍后继续。'};
let token='',me=null,rubrics=[],job=null,timer=null,imageUrl=null,noticeTimer=null,aborter=null,createKey=null,createFingerprint='',dirty=false,selectionVersion=0,selectionRequest=0,jobsOffset=0,candidatesOffset=0,jobsRequest=0,candidatesRequest=0;
let eventCursor=0,eventItems=[],eventCount=0,eventRequest=0;
let selectionAborter=null;
let editVersion=0,candidateKey='',candidateRecord=null,candidateLoaded=false,candidateLoading=false,candidateUncertain=false,candidateError='',candidateRequest=0,candidateAborter=null,candidatesAborter=null,candidateFocusId='',candidateFocusJob='';
const candidateEdits=new Map();
let candidateReviewCursor='',candidateReviewScope='';
function syncCandidateReviewControls(){$('candidate-review-nav').hidden=!['reviewer','admin'].includes(me?.role);}
const emptyCandidateFilters=()=>({status:'',classroom:'',search:''});
let appliedCandidateFilters=emptyCandidateFilters();
function candidateFilterValues(){return {status:$('candidate-filter').value,classroom:$('candidate-classroom').value,search:$('candidate-search').value.trim()};}
function setCandidateFilters(value){$('candidate-filter').value=value.status;$('candidate-classroom').value=value.classroom;$('candidate-search').value=value.search;}
function resetCandidateFilters(){appliedCandidateFilters=emptyCandidateFilters();setCandidateFilters(appliedCandidateFilters);}
let loginAborter=null,sessionGeneration=0,jobsAborter=null,learningAborter=null,learningRequest=0,libraryAborter=null,libraryRequest=0;
let mutationRecovery=null,mutationAborter=null,mutationReject=null,mutationEpoch=0;
let createRecovery=null,createAborter=null,createCancel=null,createAttempt=0;
let nextTaskAborter=null,nextTaskRequest=0,lastSavedTask=null;
let jobsSort='created';
const jobTime=new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
function resetQueueFilters(){
  $('job-search').value='';$('job-filter').value='';$('job-attention').value='';$('job-classroom').value='';
  $('job-sort').value='created';
  for(const option of $('job-filter').options)option.textContent=option.value?states[option.value]:'全部状态';
  for(const option of $('job-attention').options)option.textContent=option.value?'需要处理':'全部任务';
  renderQueueLanes();
}
function setJobClassrooms(ids){
  for(const name of ['job-classroom','candidate-classroom']){
    const select=$(name),all=node('option','全部授权班级');all.value='';select.replaceChildren(all);select.value='';
    for(const id of ids){const option=node('option',id);option.value=id;select.append(option);}
  }
}
function resetEvents(){eventRequest++;eventCursor=0;eventItems=[];eventCount=0;$('events').replaceChildren();$('events-page').textContent='';controlDisabled('events-more',true);}
function node(tag,text,cls){const element=document.createElement(tag);if(text!==undefined)element.textContent=text;if(cls)element.className=cls;return element;}
function notify(text){$('message').hidden=false;$('message').textContent=text;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>{$('message').textContent='';$('message').hidden=true;noticeTimer=null;},6000);}
function sessionEnded(){return Object.assign(Error('会话已结束。'),{name:'AbortError'});}
async function api(path,{method='GET',body,signal,idempotencyKey}={}){const sessionToken=token,session=sessionGeneration;const headers={Authorization:`Bearer ${token}`};if(idempotencyKey)headers['Idempotency-Key']=idempotencyKey;let payload=body;if(body&&!(body instanceof FormData)){headers['Content-Type']='application/json';payload=JSON.stringify(body);}const response=await fetch('/api/v1'+path,{method,headers,body:payload,signal,cache:'no-store'});if(sessionToken!==token||session!==sessionGeneration)throw sessionEnded();if(response.status===401){logout();throw Error(errors.UNAUTHENTICATED);}const data=await response.json();if(sessionToken!==token||session!==sessionGeneration)throw sessionEnded();if(!response.ok){const code=data.error?.code,error=Error(errors[code]||`操作未完成（${code||response.status}），请稍后重试或联系管理员。`);error.code=code;error.status=response.status;throw error;}return data;}
function stopPolling(){clearTimeout(timer);timer=null;if(aborter)aborter.abort();aborter=null;}
function logout(){resetMutationRecovery();resetCreateRecovery();clearNotice();cancelJobSelection();resetCandidateStatus();candidateEdits.clear();candidatesAborter?.abort();candidatesAborter=null;candidateFocusId='';candidateFocusJob='';clearReadiness();sessionGeneration++;loginAborter?.abort();loginAborter=null;libraryRequest++;libraryAborter?.abort();libraryAborter=null;learningRequest++;learningAborter?.abort();learningAborter=null;clearRevisions();clearHandoff();selectionVersion++;clearCitation();resetEvents();jobsRequest++;candidatesRequest++;jobsOffset=0;jobsSort='created';candidatesOffset=0;dirty=false;stopPolling();token='';me=null;job=null;candidateReviewCursor='';candidateReviewScope='';syncCandidateReviewControls();rubrics=[];renderNextAction();renderReassessmentSource();$('token').value='';$('login-panel').hidden=false;$('workspace').hidden=true;$('logout').hidden=true;$('identity').textContent='教师工作台';$('job-list').replaceChildren();$('report-content').replaceChildren();$('confirmed-text').value='';$('candidate-text').value='';for(const id of ['new-text','title','prompt','review-comment','provenance','candidate-comment','operator-request','feedback-comment','student-ref','experiment-id','experiment-rubric','experiment-release','experiment-model','experiment-classes'])$(id).value='';for(const id of ['knowledge-list','library-list','explanation','feedback-findings','language-hints','learning-list','experiment-list','ocr-review','events','review-scores'])$(id).replaceChildren();createKey=null;createFingerprint='';$('candidate-feedback').checked=false;$('reuse-confirmed').checked=false;$('experiment-consent').checked=false;$('cloud-consent').checked=false;$('cloud-budget').value='0';$('routing-preference').value='local';$('image-file').value='';$('upload-mode').value='append';for(const id of ['job-title','job-meta','job-status','job-error','service-status','job-progress'])$(id).textContent='';jobsAborter?.abort();jobsAborter=null;resetQueueFilters();resetCandidateFilters();setJobClassrooms([]);showPanel('create');resetCreateForm();clearImage();}
function clearImage(){currentImageHash=null;$('citation-boxes').replaceChildren();if($('image-dialog').open)$('image-dialog').close();$('large-original').removeAttribute('src');$('zoom-original').hidden=true;if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=null;$('original-image').removeAttribute('src');$('original-image').hidden=true;}
function clearNotice(){clearTimeout(noticeTimer);noticeTimer=null;$('message').hidden=true;}
function controlDisabled(id,value){$(id).dataset.stateDisabled=String(value);$(id).disabled=value;}
async function busy(button,work){
  const owner={};button.busyOwner=owner;const form=button.closest('form');if(form)form.busyOwner=owner;button.disabled=true;button.setAttribute('aria-busy','true');if(form){form.inert=true;form.setAttribute('aria-busy','true');}
  try{await work();}catch(error){if(error.name!=='AbortError')notify(error.message);}
  finally{if(form?.busyOwner===owner){form.inert=false;form.removeAttribute('aria-busy');}if(button.busyOwner===owner){button.removeAttribute('aria-busy');button.disabled=button.dataset.stateDisabled==='true';}}
}
$('login-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
  loginAborter?.abort();const controller=new AbortController();loginAborter=controller;
  const session=++sessionGeneration;token=$('token').value.trim();const sessionToken=token;
  const current=()=>loginAborter===controller&&session===sessionGeneration&&sessionToken===token;
  const check=()=>{if(!current())throw sessionEnded();};
  try{
    me=await api('/me',{signal:controller.signal});check();rubrics=(await api('/rubrics',{signal:controller.signal})).items;check();
    const health=await fetch('/health/ready',{cache:'no-store',signal:controller.signal});check();
    if(!health.ok)throw Error('服务暂不可用，请稍后重新登录。');
    const status=await health.json();check();setJobClassrooms(me.classroomIds);syncCandidateReviewControls();$('job-attention').value='needs_action';if(me.classroomIds.length===1)$('job-classroom').value=me.classroomIds[0];await refreshJobs();check();
    const classes=$('classroom');classes.replaceChildren();
    for(const id of me.classroomIds){const option=node('option',id);option.value=id;classes.append(option);}
    const choice=$('rubric');choice.replaceChildren();
    for(const r of rubrics){const option=node('option',r.title);option.value=r.id;choice.append(option);}
    const grade=Number($('grade').value),initial=rubrics.find(r=>r.gradeMin<=grade&&grade<=r.gradeMax)||rubrics[0];
    if(initial){choice.value=initial.id;alignGrade();}
    $('service-status').textContent=`评分模型：${status.modelConfigured?'已配置，实际结果以处理报告为准':'待配置'} · OCR：${status.ocrConfigured?'已配置':'可手工核对原文'}。所有评分仍需教师确认。`;
    $('identity').textContent=`${me.tenantId} · ${me.role==='teacher'?'任课教师':me.role==='reviewer'?'教研审核':'学校管理员'}`;
    $('token').value='';$('login-panel').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;
    if(!rubrics.length)notify('当前学校没有已发布量规，管理员需先配置量规。');
  }catch(error){if(current()){logout();throw error;}if(!token&&error.message===errors.UNAUTHENTICATED)throw error;throw sessionEnded();}
  finally{if(loginAborter===controller)loginAborter=null;}
});});
function alignGrade(){const r=rubrics.find(item=>item.id===$('rubric').value),grade=Number($('grade').value);if(r&&(grade<r.gradeMin||grade>r.gradeMax))$('grade').value=String(r.gradeMin);}
$('rubric').addEventListener('change',alignGrade);

function discardEdits(){if(!dirty&&!revisionDirty&&!candidateEdits.size)return true;if(!window.confirm('有尚未保存的修改，确定离开当前作业吗？'))return false;return true;}
$('logout').addEventListener('click',()=>{if(discardEdits())logout();});
$('workspace').addEventListener('input',event=>{const form=event.target.closest('form');if(form&&(['create-form','text-form','review-form','feedback-form','candidate-form','upload-form','experiment-form'].includes(form.id)||form.dataset.unsaved==='true')){dirty=true;editVersion++;if(form.dataset.candidateId)candidateEdits.set(form.dataset.candidateId,editVersion);clearReadiness();$('readiness-status').textContent='有未保存的修改，请先保存或确认后再检查报告。';}});
window.addEventListener('beforeunload',event=>{if(dirty||revisionDirty||candidateEdits.size||createRecovery&&!createRecovery.id||createAborter){event.preventDefault();event.returnValue='';}});
function updateIntentRequirements(){
  const field=$('operator-request');field.required=$('intent').value==='auto';field.setCustomValidity('');
}
$('intent').addEventListener('change',updateIntentRequirements);
$('operator-request').addEventListener('input',()=>$('operator-request').setCustomValidity(''));
updateIntentRequirements();
function resetCreateForm(){for(const id of ['title','prompt','new-text','operator-request','student-ref'])$(id).value='';$('intent').value='assess';updateIntentRequirements();$('experiment-consent').checked=false;$('cloud-consent').checked=false;$('routing-preference').value='local';$('cloud-budget').value='0';$('language-hints').replaceChildren();}
function showPanel(name){for(const panel of ['create','job','knowledge'])$(`${panel}-panel`).hidden=panel!==name;}
$('new-job').addEventListener('click',()=>{if(createRecovery&&!createRecovery.id){notify('上次提交结果尚未确认，请先按原请求处理，不要重复新建。');return;}if(!discardEdits())return;resetCreateRecovery();cancelJobSelection();resetCandidateStatus();candidateEdits.clear();clearReadiness();clearRevisions();clearHandoff();selectionVersion++;clearCitation();resetEvents();stopPolling();clearImage();job=null;resetMutationRecovery();renderNextAction();renderReassessmentSource();createKey=null;createFingerprint='';resetCreateForm();dirty=false;showPanel('create');});
$('sample-text').addEventListener('click',()=>{if(!discardEdits())return;const english=rubrics.find(r=>r.languagePolicy==='en'&&r.genre==='short-text'&&r.gradeMin<=7&&r.gradeMax>=7);if(english){$('rubric').value=english.id;$('grade').value='7';}$('title').value='A helpful afternoon';$('prompt').value='Write a short story about helping someone.';$('new-text').value='Last Saturday, I go to the park with my sister. We saw a little boy looking for his kite. My sister helped him find it behind a bench. He smiled and said thank you. I learned that helping someone can make an ordinary day special.';dirty=true;});
function renderCreateRecovery(){
 const value=createRecovery;$('create-recovery').hidden=!value;
 $('create-receipt-state').textContent=value?.message||'';$('create-receipt-label').textContent=value?(value.id?'任务编号：':'请求编号：'):'';$('create-receipt-id').textContent=value?.id||value?.key||'';
 $('create-recover').textContent=value?.id?'打开已创建任务':'按原请求重试';controlDisabled('create-recover',!value||Boolean(createAborter));controlDisabled('create-submit',Boolean(value||createAborter));
}
function resetCreateRecovery(){createAttempt++;createAborter?.abort();createCancel?.(sessionEnded());createAborter=null;createCancel=null;createRecovery=null;createKey=null;createFingerprint='';renderCreateRecovery();}
async function openCreatedReceipt(value){
 if(value!==createRecovery||!value.id||value.session!==sessionGeneration||!token)return false;
 try{const opened=await selectJob(value.id);if(value!==createRecovery||value.session!==sessionGeneration)return false;if(opened){createRecovery=null;createKey=null;createFingerprint='';renderCreateRecovery();return true;}value.message='任务已创建，当前编辑仍保留。可稍后打开这份任务。';}
 catch(error){if(value!==createRecovery||value.session!==sessionGeneration||error.name==='AbortError')return false;value.message='任务已创建，但暂时无法打开。原输入保留，可用本次回执重试打开。';}
 renderCreateRecovery();return false;
}
async function sendCreateRequest(value,explicit=false){
 if(value!==createRecovery||value.session!==sessionGeneration||createAborter||!token)return;
 const retrying=value.state==='unknown',controller=new AbortController(),attempt=++createAttempt,selection=selectionVersion,intent=selectionRequest,session=sessionGeneration;
 createAborter=controller;value.state='submitting';value.message='正在提交本次任务，请稍候…';renderCreateRecovery();let deadline;
 const same=()=>attempt===createAttempt&&session===sessionGeneration&&value===createRecovery&&Boolean(token);
 try{
  const timeout=new Promise((_,reject)=>{createCancel=reject;deadline=setTimeout(()=>{controller.abort();reject(Error('提交等待超时，结果尚未确认。'));},30000);});
  const data=await Promise.race([api('/jobs',{method:'POST',body:value.payload,idempotencyKey:value.key,signal:controller.signal}),timeout]);clearTimeout(deadline);
  if(!same())return;
  if(!data||typeof data.id!=='string'||!data.id||data.id.length>80||typeof data.classroomId==='string'&&data.classroomId!==value.payload.classroomId)throw Error('创建响应未能核对，请保留原请求。');
  value.id=data.id;value.state='accepted';value.message='任务已创建，原请求已收件。';value.payload=null;
  const unchanged=selection===selectionVersion&&intent===selectionRequest&&value.edits===editVersion;
  if(unchanged)dirty=false;renderCreateRecovery();
  if(explicit||unchanged)await openCreatedReceipt(value);
  if(session===sessionGeneration)void refreshJobs(0).catch(error=>{if(session===sessionGeneration&&error.name!=='AbortError')notify('任务已创建，作业列表暂未更新，可稍后刷新。');});
 }catch(error){
  if(!same()||error.name==='AbortError'&&session!==sessionGeneration)return;
  if(!retrying&&[400,403,422].includes(error.status)){createRecovery=null;createKey=null;createFingerprint='';notify(error.message);}
  else{value.state='unknown';value.message=(controller.signal.aborted?'提交等待超时，结果尚未确认。':error.code?error.message:'未能确认服务器是否已收件。')+' 原输入与本次请求保留，请按原请求重试，不要重复新建。';}
 }finally{clearTimeout(deadline);if(attempt===createAttempt){createAborter=null;createCancel=null;renderCreateRecovery();}}
}
$('create-recover').addEventListener('click',event=>busy(event.target,async()=>{const value=createRecovery;if(!value||createAborter||value.session!==sessionGeneration)return;if(value.id)await openCreatedReceipt(value);else await sendCreateRequest(value,true);}));
$('create-form').addEventListener('submit',e=>{
 e.preventDefault();if(createRecovery||createAborter){renderCreateRecovery();notify('先处理本次提交回执，再新建另一份任务。');return;}
 const instruction=$('operator-request');if($('intent').value==='auto'&&!instruction.value.trim()){instruction.setCustomValidity('请填写教师处理要求，或选择评分、仅校对。');instruction.reportValidity();instruction.focus();return;}instruction.setCustomValidity('');
 busy(e.submitter,async()=>{
 const rubric=rubrics.find(r=>r.id===$('rubric').value);if(!rubric)throw Error('请先选择学校已发布量规。');const text=$('new-text').value;if(Array.from(text).length>15000)throw Error('正文最多 15,000 字符。');const payload={classroomId:$('classroom').value,title:$('title').value,prompt:$('prompt').value,text,grade:Number($('grade').value),languagePolicy:rubric.languagePolicy,genre:rubric.genre,rubricId:rubric.id,intent:$('intent').value||'assess',operatorRequest:$('operator-request').value,studentRef:$('student-ref').value,experimentConsent:$('experiment-consent').checked,routingPreference:$('routing-preference').value||'local',cloudConsent:$('cloud-consent').checked,maxCostMicrousd:Math.round(Number($('cloud-budget').value||0)*1000000)};
 createFingerprint=JSON.stringify(payload);createKey=crypto.randomUUID();createRecovery={key:createKey,payload:Object.freeze(payload),session:sessionGeneration,edits:editVersion,id:null,state:'ready',message:''};await sendCreateRequest(createRecovery);
 });
});
async function refreshJobs(offset=jobsOffset){
  jobsAborter?.abort();const controller=new AbortController();jobsAborter=controller;
  const request=++jobsRequest,search=$('job-search').value.trim(),sort=$('job-sort').value||'created';
  try{
  const data=await api(`/jobs?limit=30&offset=${offset}&search=${encodeURIComponent(search)}&status=${encodeURIComponent($('job-filter').value)}&attention=${encodeURIComponent($('job-attention').value)}&classroom=${encodeURIComponent($('job-classroom').value)}&view=summary&sort=${encodeURIComponent(sort)}`,{signal:controller.signal});
  if(request!==jobsRequest)return;
  if(!Array.isArray(data.items))throw Error('作业列表暂不可用，请刷新后重试。');
  renderJobPage(data,offset);
  }catch(error){if(!controller.signal.aborted&&request===jobsRequest){$('job-sort').value=jobsSort;$('jobs-page').textContent='作业列表尚未更新，请刷新重试。';controlDisabled('jobs-prev',true);controlDisabled('jobs-next',true);throw error;}}
  finally{if(jobsAborter===controller)jobsAborter=null;}
}
function renderJobPage(data,offset=0){
  jobsSort=['created','updated'].includes(data.sort)?data.sort:($('job-sort').value||'created');$('job-sort').value=jobsSort;
  jobsOffset=offset;const counts=data.statusCounts||{};for(const option of $('job-filter').options){option.textContent=(option.value?states[option.value]:'全部状态')+'（'+(option.value?(counts[option.value]||0):Object.values(counts).reduce((a,b)=>a+b,0))+'）';}const list=data.items;$('job-list').replaceChildren();
  for(const option of $('job-attention').options){const count=option.value?(data.actionRequiredCount??['needs_text_review','teacher_review','failed'].reduce((sum,state)=>sum+(counts[state]||0),0)):Object.values(counts).reduce((a,b)=>a+b,0);option.textContent=(option.value?'需要处理':'全部任务')+'（'+count+'）';}
  renderQueueLanes(counts,data.actionRequiredCount);renderBatchContinuation();
  controlDisabled('jobs-prev',offset===0);controlDisabled('jobs-next',!data.hasMore);
  $('jobs-page').textContent=list.length?`第 ${offset+1}–${offset+list.length} 条`:'没有符合条件的作业';
  if(!list.length)$('job-list').append(node('p','没有符合当前班级、题目和状态的作业，可清除筛选或创建任务。','hint'));
  for(const item of list){const b=node('button',item.payload?.title||'未命名作业',`job-item${job?.id===item.id?' active':''}`);b.dataset.jobId=item.id;if(job?.id===item.id)b.setAttribute('aria-current','page');b.append(node('small',`${item.classroomId} · ${states[item.status]||item.status}`));if(Object.hasOwn(queueActions,item.queueAction))b.append(node('small',queueActions[item.queueAction]));const date=new Date(jobsSort==='updated'?item.updatedAt:item.createdAt);if(Number.isFinite(date.getTime()))b.append(node('small',`${jobsSort==='updated'?'更新':'创建'} ${jobTime.format(date)}`));b.addEventListener('click',()=>busy(b,()=>selectJob(item.id)));$('job-list').append(b);}
}
function renderQueueLanes(counts={},actionCount){
 const total=Object.values(counts).reduce((a,b)=>a+b,0),attention=$('job-attention').value,status=$('job-filter').value;
 for(const [id,label,count,pressed]of [['attention','需要处理',actionCount??['needs_text_review','teacher_review','failed'].reduce((sum,key)=>sum+(counts[key]||0),0),attention==='needs_action'&&!status],['approved','教师已确认',counts.approved||0,!attention&&status==='approved'],['all','全部任务',total,!attention&&!status]]){
  $('lane-'+id).textContent=label+'（'+count+'）';$('lane-'+id).setAttribute('aria-pressed',String(pressed));
 }
 const advancedCount=Number(Boolean(attention))+Number(Boolean(status))+Number($('job-sort').value==='updated');$('queue-filter-indicator').textContent=advancedCount?'已选 '+advancedCount+' 项':'按需筛选';
}
for(const lane of ['attention','approved','all'])$('lane-'+lane).addEventListener('click',event=>busy(event.target,async()=>{
 $('job-attention').value=lane==='attention'?'needs_action':'';$('job-filter').value=lane==='approved'?'approved':'';await refreshJobs(0);
}));
function nextTaskScope(){return {classroom:$('job-classroom').value||job?.classroomId||'',search:$('job-search').value.trim(),attention:$('job-attention').value,status:$('job-filter').value,sort:$('job-sort').value||'created'};}
function renderBatchContinuation(){
 const scope=nextTaskScope();$('batch-continuation').hidden=!job;
 $('next-task-context').textContent=job?'在班级 '+scope.classroom+(scope.search?' 的当前题目查找范围':'')+' 内继续；当前作业仍会保留。':'';
 if(!nextTaskAborter)$('next-task-status').textContent=lastSavedTask&&lastSavedTask.id===job?.id&&lastSavedTask.version===job?.version?lastSavedTask.message:['approved','published'].includes(job?.status)?'当前任务已由教师确认，可继续处理其他作业。':'可以先完成当前任务，再继续下一份；未保存的编辑会在离开前提示。';
}
function continuationIdentity(){return {id:job?.id,version:job?.version,selection:selectionVersion,session:sessionGeneration};}
function savedTaskContinuation(message,context){if(context&&JSON.stringify(context)!==JSON.stringify(continuationIdentity()))return;lastSavedTask={id:job.id,version:job.version,message};renderBatchContinuation();$('next-pending-task').focus({preventScroll:true});$('batch-continuation').scrollIntoView({block:'nearest'});}
function cancelNextPendingTask(){nextTaskRequest++;nextTaskAborter?.abort();nextTaskAborter=null;lastSavedTask=null;$('next-task-status').textContent='';}
async function openNextPendingTask(){
 if(!job||!token||nextTaskAborter||!discardEdits())return false;
 const scope=nextTaskScope();if(!scope.classroom){$('next-task-status').textContent='请先选择一个班级。';return false;}
 const snapshot={id:job.id,version:job.version,selection:selectionVersion,intent:selectionRequest,session:sessionGeneration,edits:editVersion},controller=new AbortController(),request=++nextTaskRequest;const deadline=setTimeout(()=>controller.abort(),15000);nextTaskAborter=controller;$('next-task-status').textContent='正在查找下一份待处理作业…';
 try{
  const data=await api('/jobs?limit=30&offset=0&attention=needs_action&view=summary&classroom='+encodeURIComponent(scope.classroom)+'&search='+encodeURIComponent(scope.search)+'&sort='+encodeURIComponent(scope.sort),{signal:controller.signal});
  if(request!==nextTaskRequest||snapshot.id!==job?.id||snapshot.version!==job?.version||snapshot.selection!==selectionVersion||snapshot.intent!==selectionRequest||snapshot.session!==sessionGeneration||JSON.stringify(scope)!==JSON.stringify(nextTaskScope())){if(snapshot.session===sessionGeneration&&job)$('next-task-status').textContent='任务或筛选已变化，请重新查找。';return false;}
  if(snapshot.edits!==editVersion){$('next-task-status').textContent='查找期间有新修改，已保留当前作业。请保存后再继续。';return false;}
  if(!Array.isArray(data.items))throw Error('待处理列表暂不可用，请重试。');
  const next=data.items.find(item=>item.id!==job.id&&item.classroomId===scope.classroom&&['needs_text_review','teacher_review','failed'].includes(item.status));
  if(!next){$('next-task-status').textContent=data.hasMore?'当前页没有其他待处理任务，可在作业列表继续查找。':'当前班级与题目范围内没有其他待处理作业。';return false;}
  const opened=await selectJob(next.id);if(opened&&job?.id===next.id){jobsRequest++;jobsAborter?.abort();$('job-attention').value='needs_action';$('job-filter').value='';renderJobPage({...data,items:data.items.filter(item=>item.classroomId===scope.classroom)},0);$('next-task-status').textContent='已打开下一份待处理作业，请核对当前任务后继续。';}return opened;
 }catch(error){if(error.name!=='AbortError')$('next-task-status').textContent=error.message;else if(request===nextTaskRequest&&job)$('next-task-status').textContent='查找超时或已取消，当前作业保留，可重试。';return false;}
 finally{clearTimeout(deadline);if(nextTaskAborter===controller)nextTaskAborter=null;}
}
$('next-pending-task').addEventListener('click',event=>busy(event.target,()=>openNextPendingTask()));
$('job-filter').addEventListener('change',e=>busy(e.target,()=>refreshJobs(0)));
$('job-attention').addEventListener('change',e=>busy(e.target,()=>refreshJobs(0)));
$('job-classroom').addEventListener('change',e=>busy(e.target,()=>refreshJobs(0)));
$('job-sort').addEventListener('change',e=>busy(e.target,()=>refreshJobs(0)));
$('jobs-reset').addEventListener('click',e=>{resetQueueFilters();busy(e.target,()=>refreshJobs(0));});
$('refresh-jobs').addEventListener('click',e=>busy(e.target,()=>refreshJobs(0)));
$('jobs-prev').addEventListener('click',e=>busy(e.target,()=>refreshJobs(Math.max(0,jobsOffset-30))));
$('jobs-next').addEventListener('click',e=>busy(e.target,()=>refreshJobs(jobsOffset+30)));
$('job-search-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,()=>refreshJobs(0));});
function cancelJobSelection(){
  abortMutationWait();
  cancelNextPendingTask();
  selectionRequest++;selectionAborter?.abort();selectionAborter=null;
  $('job-panel').inert=false;$('job-panel').removeAttribute('aria-busy');
}
async function selectJob(id){
  if(!discardEdits())return false;abortMutationWait();
  const request=++selectionRequest,previous=selectionVersion,session=sessionGeneration,panel=$('job-panel');
  selectionAborter?.abort();const controller=new AbortController();selectionAborter=controller;
  stopPolling();panel.inert=true;panel.setAttribute('aria-busy','true');
  let timedOut=false;const deadline=setTimeout(()=>{timedOut=true;controller.abort();},30000);
  try{
    const next=await api(`/jobs/${id}`,{signal:controller.signal});clearTimeout(deadline);
    if(request!==selectionRequest||previous!==selectionVersion||session!==sessionGeneration)return false;
    if(next?.id!==id||!next.payload||typeof next.payload!=='object'||Array.isArray(next.payload))throw Error('作业响应与选择不一致，请刷新后重试。');
    const selection=++selectionVersion;
    // Only commit the navigation after a matching authorized read. A failed
    // request keeps current edits, image and revision context available.
    resetMutationRecovery();clearNotice();resetCandidateStatus();candidateEdits.clear();clearRevisions();clearHandoff();clearReadiness();clearCitation();clearImage();job=next;dirty=false;resetEvents();
    $('job-list').querySelectorAll('[data-job-id]').forEach(button=>{const current=button.dataset.jobId===id;button.classList.toggle('active',current);if(current)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
    $('review-comment').value=job.review?.comment||'';$('candidate-text').value='';$('candidate-comment').value='';$('candidate-feedback').checked=false;$('provenance').value='';$('reuse-confirmed').checked=false;$('image-file').value='';$('upload-mode').value='append';
    panel.inert=false;panel.removeAttribute('aria-busy');
    showPanel('job');$('explanation').replaceChildren();$('feedback-comment').value='';renderJob(true);
    setJobStage(job.status==='needs_text_review'||job.status==='failed'&&!job.textVersion?'source':job.result?'report':'records',false);$('job-title').focus({preventScroll:true});$('job-title').scrollIntoView({block:'start',behavior:'auto'});
    try{await loadEvents();if(selection===selectionVersion&&job.hasImage)await loadImage();}
    catch(error){if(error.name!=='AbortError'&&selection===selectionVersion)notify(error.message);}
    finally{if(selection===selectionVersion)schedulePoll();}
    return request===selectionRequest&&selection===selectionVersion&&job?.id===id;
  }catch(error){
    if(request===selectionRequest&&previous===selectionVersion&&session===sessionGeneration&&token)schedulePoll();
    throw timedOut?Error('作业读取超时，当前编辑仍保留，请稍后重试。'):error;
  }finally{clearTimeout(deadline);if(selectionAborter===controller)selectionAborter=null;if(request===selectionRequest){panel.inert=false;panel.removeAttribute('aria-busy');}}
}

function mutationOwnerCurrent(record){return Boolean(job?.id===record.id&&job.version===record.version&&sessionGeneration===record.session);}
function mutationContext(record){return Boolean(job?.id===record.id&&job.version===record.version&&selectionVersion===record.selection&&selectionRequest===record.intent&&sessionGeneration===record.session);}
function renderMutationRecovery(){
 const value=mutationRecovery,current=value&&job?.id===value.id&&sessionGeneration===value.session;$('mutation-recovery').hidden=!current;if(!current){$('mutation-state').textContent='';$('mutation-observed').textContent='';return;}
 $('mutation-state').textContent=value.message;$('mutation-observed').textContent=value.observed?'上次读取版本 '+value.observed.version+' · '+(states[value.observed.status]||'状态未知')+'。请核对保存内容与处理记录，不以版本变化代替本次操作完成。':'';
 $('mutation-stop').hidden=!mutationAborter;controlDisabled('mutation-read',Boolean(mutationAborter)||!mutationOwnerCurrent(value));$('mutation-open').hidden=!value.observed;controlDisabled('mutation-open',Boolean(mutationAborter));$('mutation-keep').hidden=!value.observed||value.observed.version!==value.version;controlDisabled('mutation-keep',Boolean(mutationAborter)||!mutationContext(value));
}
function abortMutationWait(){if(!mutationAborter)return;const value=mutationRecovery;if(value){value.state='unknown';value.message='本机等待已停止，服务端结果尚未确认。请先读取当前任务状态。';}mutationAborter.abort();mutationReject?.(sessionEnded());renderMutationRecovery();}
function resetMutationRecovery(){mutationEpoch++;abortMutationWait();mutationAborter=null;mutationReject=null;mutationRecovery=null;renderMutationRecovery();}
async function boundedJobRequest(path,options={},timeoutMs=30000){
 const controller=new AbortController(),epoch=++mutationEpoch;mutationAborter=controller;renderMutationRecovery();let deadline;
 try{const expired=new Promise((_,reject)=>{mutationReject=reject;deadline=setTimeout(()=>{controller.abort();reject(Error('等待超时'));},timeoutMs);});return await Promise.race([api(path,{...options,signal:controller.signal}),expired]);}
 finally{clearTimeout(deadline);if(epoch===mutationEpoch){mutationAborter=null;mutationReject=null;renderMutationRecovery();}}
}
async function mutateCurrentJob(kind,options){
 if(!job)return false;if(mutationAborter||mutationRecovery?.id===job.id)throw Error('上次操作结果尚未核对，请先读取当前任务状态，不要重复提交。');
 const record={id:job.id,version:job.version,selection:selectionVersion,intent:selectionRequest,session:sessionGeneration,edits:editVersion,state:'waiting',observed:null,message:'正在提交本次操作，请稍候…'};mutationRecovery=record;stopPolling();renderMutationRecovery();
 try{const next=await boundedJobRequest(`/jobs/${record.id}/${kind}`,options,options?.body instanceof FormData?90000:30000);if(!mutationContext(record))return false;
  if(!next||next.id!==record.id||!Number.isSafeInteger(next.version)||!next.payload)throw Error('操作响应未能核对');
  if(record.edits!==editVersion){record.state='unknown';record.observed={version:next.version,status:next.status};record.message='服务端已返回新版本，当前新增编辑仍保留。请先核对已保存状态。';renderMutationRecovery();return false;}
  job=next;mutationRecovery=null;renderMutationRecovery();return true;
 }catch(error){if(!mutationContext(record))return false;if([400,403,422].includes(error.status)||(error.status===507&&['image','pages','page-replacement'].includes(kind)&&['STORAGE_CAPACITY_EXCEEDED','IMAGE_STORAGE_CAPACITY_EXCEEDED'].includes(error.code))){mutationRecovery=null;renderMutationRecovery();schedulePoll();throw error;}record.state='unknown';record.message=(['IMAGE_STORAGE_INVALID','IMAGE_STORAGE_WRITE_FAILED'].includes(error.code)?errors[error.code]+' ':'')+'本次操作结果尚未确认，原输入与原图保留。请先读取当前任务状态，不要直接重复提交。';renderMutationRecovery();return false;}
}
$('mutation-stop').addEventListener('click',abortMutationWait);
$('mutation-read').addEventListener('click',event=>busy(event.target,async()=>{const value=mutationRecovery;if(!value||mutationAborter||!mutationOwnerCurrent(value))return;value.selection=selectionVersion;value.intent=selectionRequest;value.message='正在读取服务器当前状态，输入保持…';renderMutationRecovery();try{const next=await boundedJobRequest(`/jobs/${value.id}`,{},15000);if(!mutationContext(value))return;if(next?.id!==value.id||!Number.isSafeInteger(next.version))throw Error('状态响应未能核对');value.observed={version:next.version,status:next.status};value.message=next.version===value.version?'服务器仍显示原版本，原输入保留。核对后可以明确继续，不会自动重发。':'服务器已有新版本，原输入保留。请先核对；如要打开保存版本，未提交修改仍会提示。';}catch(error){if(mutationContext(value))value.message='本次状态读取未完成，输入仍保留，可稍后重试。';}finally{renderMutationRecovery();}}));
$('mutation-open').addEventListener('click',event=>busy(event.target,async()=>{const value=mutationRecovery;if(value?.observed&&!mutationAborter&&mutationOwnerCurrent(value))await selectJob(value.id);}));
$('mutation-keep').addEventListener('click',()=>{const value=mutationRecovery;if(!value?.observed||mutationAborter||!mutationContext(value)||value.observed.version!==value.version)return;mutationRecovery=null;renderMutationRecovery();schedulePoll();notify('当前输入保留，可明确重新提交；未自动重发旧操作。');});
function renderUploadIntent(){
 const needs=job?.status==='failed'&&job.textVersion===0&&job.hasImage,key=job?`${selectionVersion}:${job.id}:${job.version}:${needs}`:'';
 if($('upload-mode').dataset.choiceFor!==key){$('upload-mode').dataset.choiceFor=key;$('upload-mode').value=needs?'choose':'append';}
 $('upload-mode').querySelector('option[value=choose]').hidden=!needs;
 $('upload-intent-state').textContent=needs?(job.errorCode==='OCR_CACHED_CONFIGURATION_CHANGED'?'已有转写结果保留，请先核对处理记录与配置，不要为恢复旧配置而直接替换原稿。':'已有原稿。修正当前有效页请选择替换，原页归档保留；不同的新页请选择追加。先核对当前页再上传。'):'';
}

function selectedImage(){const value=$('image-page').value||'1';return {archived:value.startsWith('archive:'),page:Number(value.replace('archive:',''))||1};}
async function loadImage(){
  const id=job.id,sessionToken=token,selection=selectionVersion,view=$('image-page').value,selected=selectedImage();clearImage();
  const response=await fetch(`/api/v1/jobs/${id}/image?page=${selected.page}&archived=${selected.archived}`,{headers:{Authorization:`Bearer ${token}`},cache:'no-store'});
  if(job?.id!==id||selection!==selectionVersion||sessionToken!==token||view!==$('image-page').value)return;
  if(response.status===401){logout();throw Error(errors.UNAUTHENTICATED);}
  if(!response.ok)throw Error(errors.IMAGE_UNAVAILABLE);
  const blob=await response.blob();if(job?.id!==id||selection!==selectionVersion||sessionToken!==token||view!==$('image-page').value)return;
  const imageHash=globalThis.crypto?.subtle?Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer())),byte=>byte.toString(16).padStart(2,'0')).join(''):null;
  if(job?.id!==id||selection!==selectionVersion||sessionToken!==token||view!==$('image-page').value)return;
  clearImage();currentImageHash=imageHash;imageUrl=URL.createObjectURL(blob);$('original-image').src=imageUrl;$('original-image').hidden=false;$('zoom-original').hidden=false;await $('original-image').decode().catch(()=>{});drawCitationBoxes();
}
function updatePageButtons(){const pages=job?.ocr?.pages||[],selected=selectedImage();controlDisabled('page-up',selected.archived||job?.status!=='needs_text_review'||selected.page<=1);controlDisabled('page-down',selected.archived||job?.status!=='needs_text_review'||selected.page>=pages.length);$('upload-mode').disabled=selected.archived||!job?.hasImage;if(selected.archived)$('upload-mode').value='append';}
function renderPageSelector(){
  const select=$('image-page'),current=select.value||'1',values=[],pages=job.ocr?.pages||[];select.replaceChildren();
  for(let i=0;i<Math.max(1,pages.length);i++){const option=node('option',`第 ${i+1} 页`);option.value=String(i+1);values.push(option.value);select.append(option);}
  for(const [i,page] of (job.ocr?.supersededPages||[]).entries()){const option=node('option',`替换前原稿 ${i+1}（原第 ${page.page} 页）`);option.value=`archive:${i+1}`;values.push(option.value);select.append(option);}
  select.value=values.includes(current)?current:'1';updatePageButtons();
}
function updateProgress(stage=''){
  const active=job&&['queued','running','ocr_queued','ocr_running'].includes(job.status),root=$('job-progress');root.hidden=!active;
  root.textContent=active?`${stage||(['ocr_queued','ocr_running'].includes(job.status)?'正在处理原稿识别':job.status==='queued'?'任务已排队':'正在评阅确认后的原文')}。较长任务可能需要几分钟；可离开本页后再回来查看，关闭页面不会取消已提交任务。`:'';
}
function renderJob(fillText=false){renderMutationRecovery();renderBatchContinuation();if(citation&&(citation.jobVersion!==job.version||citation.textHash!==job.textHash))clearCitation();if(handoffIdentity&&handoffIdentity!==currentHandoffIdentity())clearHandoff();updateProgress();$('job-title').textContent=job.payload.title;$('job-status').textContent=states[job.status]||job.status;$('job-meta').textContent=`${job.classroomId} · ${job.payload.grade} 年级 · 文字版本 ${job.textVersion} · ${job.rubric?.title||'待确认量规'}`;$('job-error').hidden=!job.errorCode;$('job-error').textContent=errors[job.errorCode]||`处理未完成（${job.errorCode||''}），已保留当前状态。`;$('retry-job').hidden=job.status!=='failed';controlDisabled('cancel-job',['cancelled','approved','published','erased'].includes(job.status));$('upload-form').hidden=!(job.status==='needs_text_review'||(job.status==='failed'&&job.textVersion===0));renderPageSelector();renderUploadIntent();const canEditText=['needs_text_review','failed','teacher_review'].includes(job.status);$('text-form').hidden=false;$('confirmed-text').readOnly=!canEditText;$('confirm-checkbox').disabled=!canEditText;$('confirm-checkbox').closest('label').hidden=!canEditText;$('text-form').querySelector('button').hidden=!canEditText;$('text-form').querySelector('button').textContent=job.payload.intent==='proofread'?'确认原文并开始校对':'确认原文并开始评分';if(fillText){$('confirmed-text').value=job.text;$('confirm-checkbox').checked=false;updateLength();}const ocr=$('ocr-review');ocr.replaceChildren();if(job.ocr?.text){if(job.ocr.layoutWarnings?.length)ocr.append(node('p','原稿可能含分栏或文字重叠，请放大核对阅读顺序、涂改和删除线，不要仅依据转写。','notice'));if(job.ocr.missingPageSuspected)ocr.append(node('p','页码疑似不完整，请补页或校正误识的页码后确认。','notice'));for(const item of job.ocr.disagreements||[])ocr.append(node('p',`第 ${item.page||1} 页存在识别分歧：${item.primary} / ${item.secondary}。请对照原图选择真实文字。`,'ocr-line'));ocr.append(node('p',`识别完成：${job.ocr.engine}。请按原图检查文字顺序、漏字与涂改。`,'hint'));for(const index of (job.ocr.uncertainLines||[]).slice(0,100))ocr.append(node('p',`需核对：${job.ocr.lines[index].text}`,'ocr-line'));for(const item of job.ocr.lexiconSuggestions||[])ocr.append(node('p',`词库候选：${item.incorrect} → ${item.candidate}。仅供对照原稿，保留学生真正写错的内容。`,'ocr-line'));}renderReport();renderNextAction();renderReassessmentSource();renderRevisionAvailability();renderReadiness();syncCandidateStatus();}
function updateLength(){clearCitation();$('text-length').textContent=`${Array.from($('confirmed-text').value).length} / 15,000 字符`;$('confirm-checkbox').checked=false;}
$('confirmed-text').addEventListener('input',updateLength);
$('upload-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
  const files=Array.from($('image-file').files);
  if(!files.length||files.length>6||files.some(file=>file.size>10*1024*1024)||files.reduce((n,file)=>n+file.size,0)>30*1024*1024)throw Error('请选择最多6页图片，每页10MB以内、合计30MB以内。');
  if($('confirmed-text').value!==job.text&&!window.confirm('上传会更新转写，当前未确认的文字修改将被替换。是否继续？'))return;
  if(!['append','replace'].includes($('upload-mode').value))throw Error('请明确选择追加新页或替换当前页，原稿仍保留。');
  const replacement=$('upload-mode').value==='replace',selected=selectedImage();
  if(replacement&&(files.length!==1||selected.archived))throw Error('替换时请选择当前有效页，并只上传一张新图片。');
  const body=new FormData(),kind=replacement?'page-replacement':files.length===1&&!job.hasImage?'image':'pages';body.append('version',String(job.version));
  if(replacement)body.append('page',String(selected.page));
  for(const file of files)body.append(kind==='pages'?'pages':'image',file);
  if(!await mutateCurrentJob(kind,{method:'POST',body}))return;
  dirty=false;renderJob();await loadImage();schedulePoll();
});});
$('image-page').addEventListener('change',e=>busy(e.target,async()=>{await loadImage();updatePageButtons();}));
async function movePage(delta){
  if(!discardEdits())return;
  const selected=selectedImage();if(selected.archived)return;
  const pages=job.ocr?.pages||[],index=selected.page-1,other=index+delta;
  if(other<0||other>=pages.length)return;
  const ids=pages.map(page=>page.imageId);[ids[index],ids[other]]=[ids[other],ids[index]];
  if(!await mutateCurrentJob('page-order',{method:'POST',body:{version:job.version,imageIds:ids}}))return;
  dirty=false;$('image-page').value=String(other+1);renderJob();await loadImage();schedulePoll();
}
$('page-up').addEventListener('click',e=>busy(e.target,()=>movePage(-1)));
$('page-down').addEventListener('click',e=>busy(e.target,()=>movePage(1)));

$('text-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{const text=$('confirmed-text').value;if(!text.trim()||Array.from(text).length>15000)throw Error('请填写 1–15,000 字符的有效原文。');if(!await mutateCurrentJob('text-confirmations',{method:'POST',body:{version:job.version,text,pageOrderConfirmed:$('confirm-checkbox').checked}}))return;const saved=continuationIdentity();dirty=false;renderJob();schedulePoll();await refreshJobs();savedTaskContinuation('原文已确认，当前状态：'+(states[job.status]||job.status)+'。',saved);});});
$('cancel-job').addEventListener('click',e=>busy(e.target,async()=>{if(!await mutateCurrentJob('cancel',{method:'POST',body:{version:job.version}}))return;stopPolling();renderJob();await loadEvents();await refreshJobs();}));
$('retry-job').addEventListener('click',e=>busy(e.target,async()=>{if(!await mutateCurrentJob('retries',{method:'POST',body:{version:job.version}}))return;renderJob();schedulePoll();}));
function renderReport(){
  const root=$('report-content');root.replaceChildren();$('review-scores').replaceChildren();
  const feedbackOnly=job.result?.kind==='feedback';$('review-form').hidden=job.status!=='teacher_review'||feedbackOnly;$('feedback-form').hidden=job.status!=='teacher_review'||!feedbackOnly;$('candidate-form').hidden=feedbackOnly||!['approved','published'].includes(job.status);$('explain-report').disabled=!job.result;$('withdraw-experiment').hidden=!job.result?.experiment||job.experimentParticipation==='withdrawn';
  if(!job.result){root.append(node('p',['queued','running'].includes(job.status)?'正在评阅，完成后自动更新。':'确认原文后开始评阅。','hint'));return;}
  const feedback=job.result.kind==='feedback',items=feedback?job.result.findings:job.result.scores;
  if(!Array.isArray(items)||items.length>(feedback?12:5)||items.some(item=>!item||typeof item!=='object'||(feedback?(!item.evidence||typeof item.evidence.quote!=='string'):(!Array.isArray(item.evidence)||typeof item.dimension!=='string')))||(!feedback&&job.result.warnings!==undefined&&!Array.isArray(job.result.warnings))||job.review&&(!feedback&&(!job.review.scores||typeof job.review.scores!=='object'))){
    root.append(node('p','保存的报告结构暂不可显示，请使用报告就绪检查核对；原记录保留。','notice'));$('review-form').hidden=true;$('feedback-form').hidden=true;$('candidate-form').hidden=true;return;
  }
  if(job.experimentParticipation==='withdrawn')root.append(node('p','已退出后续对照统计，既有评分保留。','hint'));
  const evaluation=node('details',undefined,'evaluation-details');evaluation.append(node('summary','模型与对照信息'));
  if(job.result.modelRoute){const route=job.result.modelRoute;evaluation.append(node('p',`本次模型：${route.model} · ${route.location==='local'?'本机':'经授权的云端'} · 费用上限估计 $${(route.estimatedMaximumCostMicrousd/1000000).toFixed(4)}`,'hint'));}
  if(feedbackOnly){
    if(evaluation.children.length>1)root.append(evaluation);
    root.append(node('h3','校对建议 · 不产生分数'));$('feedback-findings').replaceChildren();$('proof-feedback-confirmed').checked=false;
    const findings=job.review?.findings||job.result.findings||[];
    for(const [index,item] of findings.entries()){
      root.append(node('blockquote',item.evidence.quote),citationButton(item.evidence),node('p',item.suggestion));
      if(!job.review){const label=node('label',undefined,'check'),check=document.createElement('input');check.type='checkbox';check.checked=true;check.id=`proof-include-${index}`;label.append(check,node('span','发布这条建议'));const area=document.createElement('textarea');area.id=`proof-suggestion-${index}`;area.maxLength=700;area.value=item.suggestion;$('feedback-findings').append(label,area);}
    }
    if(!findings.length)root.append(node('p','未发现可发布的具体问题，仍请教师核对。','hint'));
    if(job.review)root.append(node('p',job.review.comment));return;
  }
  const overview=node('div',undefined,'report-overview'),scoreBlock=node('div',undefined,'report-score-block'),context=node('div',undefined,'report-overview-context');
  scoreBlock.append(node('p',job.review?'教师终评':'评分建议','report-score-label'),node('div',`${job.review?.total??job.result.total} / 100`,'score-total'));
  context.append(node('p',job.review?'教师已确认':'模型建议 · 待教师复核','report-confirmation'),node('p',`${job.result.scores.length} 个评分维度 · 按当前量规核对原文依据`,'hint'));
  overview.append(scoreBlock,context);root.append(overview);
  const warnings={LIVE_WEIGHT_EXPERIMENT:'本次模型建议使用已批准的对照聚合权重；教师终分仍按原量规确认。',LANGUAGE_DETECTION_UNCERTAIN:'部分片段的语言无法可靠识别，请教师核对。',OUTSIDE_LANGUAGE_REQUIRES_TEACHER:'正文含当前规则范围外的语言片段，需教师检查。',LANGUAGE_DETECTOR_UNAVAILABLE:'本机语言识别组件未安装，目前仅有字符提示。',POSSIBLE_DUPLICATE_FEEDBACK_TEACHER_REVIEW:'多个维度可能重复指出同一问题，请核对并避免重复扣分。',SYNTHETIC_RUBRIC_NOT_SCHOOL_STANDARD:'使用合成示例量规，不代表学校正式标准。',TEACHER_ARBITRATION_REQUIRED:'存在评分分歧，请教师重点复核。',REFLECTION_UNAVAILABLE_TEACHER_REVIEW:'反思复核未完成，请教师逐项审核。',REVISION_UNAVAILABLE_TEACHER_REVIEW:'自动修订未完成，保留已验证初评，请教师裁决。',PARAGRAPH_REASONING_REQUIRES_REVIEW:'原文只显示一个段落，请核对模型关于多段结构的解释。'};
  if(!job.review&&job.result.experiment?.mode==='shadow'&&job.result.experiment.status==='active')evaluation.append(node('p',`影子对照候选总分：${job.result.experiment.shadowTotal}，未替换当前评分建议。`,'hint'));
  if(!job.review&&job.result.calibration?.confidence!==null&&job.result.calibration?.confidence!==undefined){const c=job.result.calibration;evaluation.append(node('p',`校准参考：${c.alignedTotal} / 100；落入已验证误差范围的估计概率 ${(c.confidence*100).toFixed(1)}%。仍需教师最终确认。`,'hint'));}
  if(evaluation.children.length>1)root.append(evaluation);
  if(!job.review)for(const message of new Set((job.result.warnings||[]).map(warning=>warnings[warning]||'部分参考检索不可用或已被过滤，评分依据保留当前量规。')))root.append(node('p',message,'notice'));
  for(const [index,score] of job.result.scores.entries()){
    const needsRewrite=!job.review&&job.feedbackIssues?.[score.dimension]==='NO_CHANGE_CORRECTION';
    const feedback=job.review?job.review.feedback?.[score.dimension]:score;
    const included=!needsRewrite&&feedback&&feedback.include!==false,evidence=included?(feedback.evidence||[]):[];
    const details=node('details',undefined,'report-dimension'),summary=node('summary');
    details.open=index===0||needsRewrite||Boolean(included&&!evidence.length);
    summary.append(node('span',labels[score.dimension],'dimension-label'),node('span',`${job.review?.scores[score.dimension]??score.band} / 4`,'dimension-score'),node('small',included?(evidence.length?`${evidence.length} 处原文引用`:'未附原文引用'):(needsRewrite?'评语待教师重写':'评语见教师总评'),'dimension-citations'));
    const article=node('article');
    if(!job.review&&score.languageScores)article.append(node('p',score.languageScores.map(item=>`${item.language==='zh'?'中文':'英文'}片段：${item.band}/4`).join('；')+'。按当前语言策略加权后归入最近量规档位，供教师复核。','hint'));
    if(needsRewrite)article.append(node('p','这条自动评语的修改前后没有变化，请教师重写后再选择发布。评分建议和原文保留。','notice'));
    else if(feedback&&feedback.include!==false){article.append(node('p',feedback.reason));for(const evidence of feedback.evidence||[])article.append(node('blockquote',`“${evidence.quote}”`),node('small',evidence.ruleId),citationButton(evidence));if(feedback.suggestion)article.append(node('p',feedback.suggestion));}
    else article.append(node('p','本维度未发布单独评语，请看教师总评。','hint'));
    details.append(summary,article);root.append(details);
  }
  if(job.review){root.append(node('h3','教师总评'),node('p',job.review.comment));return;}
  $('feedback-confirmation').checked=false;
  for(const score of job.result.scores){
    const needsRewrite=job.feedbackIssues?.[score.dimension]==='NO_CHANGE_CORRECTION';
    const section=node('section',undefined,'feedback-edit'),label=node('label',labels[score.dimension],'band-row'),input=document.createElement('input');
    input.type='number';input.min='0';input.max='4';input.step='1';input.required=true;input.value=String(score.band);input.dataset.dimension=score.dimension;label.append(input);section.append(label);
    if(needsRewrite)section.append(node('p','原自动评语未给出实际修改。请填写依据和建议，再勾选发布本维度的评语。','notice'));
    const include=node('label',undefined,'check'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=!needsRewrite;checkbox.id=`include-${score.dimension}`;include.append(checkbox,node('span','发布本维度的评语'));section.append(include);
    for(const [key,title] of [['reason','评定依据'],['suggestion','给学生的建议']]){const field=node('label',title),area=document.createElement('textarea');area.id=`${key}-${score.dimension}`;area.maxLength=1200;area.rows=3;area.value=needsRewrite?'':score[key];field.append(area);section.append(field);}
    const citation=node('label',undefined,'check'),keep=document.createElement('input');keep.type='checkbox';keep.checked=true;keep.id=`cite-${score.dimension}`;citation.append(keep,node('span','保留上方已核对的原文引用'));section.append(citation);$('review-scores').append(section);
  }
}
$('review-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
  const scores={},feedback={};$('review-scores').querySelectorAll('input[type=number]').forEach(input=>scores[input.dataset.dimension]=Number(input.value));
  for(const score of job.result.scores){const dimension=score.dimension;feedback[dimension]={include:$(`include-${dimension}`).checked,reason:$(`reason-${dimension}`).value,suggestion:$(`suggestion-${dimension}`).value,evidence:$(`cite-${dimension}`).checked?score.evidence:[]};}
  if(!await mutateCurrentJob('reviews',{method:'POST',body:{version:job.version,scores,comment:$('review-comment').value,feedback,feedbackConfirmed:$('feedback-confirmation').checked}}))return;const saved=continuationIdentity();dirty=false;renderJob();await refreshJobs();savedTaskContinuation('本次教师评分与所选评语已保存。可继续下一份，或留在这里查看报告。',saved);notify('教师最终评分与已选评语已保存，未选模型评语不会发布。');
});});
function candidateIdentity(){return job?`${sessionGeneration}:${selectionVersion}:${job.id}:${job.version}`:'';}
function candidateContext(){return {id:job?.id,version:job?.version,selection:selectionVersion,intent:selectionRequest,session:sessionGeneration};}
function currentCandidateContext(context){return context.id===job?.id&&context.version===job?.version&&context.selection===selectionVersion&&context.intent===selectionRequest&&context.session===sessionGeneration;}
function validCandidateReceipt(value,context){return value&&typeof value.id==='string'&&/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(value.id)&&value.jobId===context.id&&value.reportVersion===context.version&&Number.isSafeInteger(value.version)&&value.version>0&&Object.hasOwn(candidateStates,value.status);}
async function candidateApi(path,options={},controller=new AbortController()){
 const deadline=setTimeout(()=>controller.abort(),30000);
 try{return await api(path,{...options,signal:controller.signal});}
 catch(error){if(controller.signal.aborted)throw Error('候选操作结果未确认，请重新读取状态；不会自动重发。');throw error;}
 finally{clearTimeout(deadline);}
}
function resetCandidateStatus(){candidateRequest++;candidateAborter?.abort();candidateAborter=null;candidateKey='';candidateRecord=null;candidateLoaded=false;candidateLoading=false;candidateUncertain=false;candidateError='';renderCandidateStatus();}
function syncCandidateStatus(){
 const available=!$('candidate-form').hidden&&Boolean(job?.review?.scores),key=available?candidateIdentity():'';
 if(key!==candidateKey){resetCandidateStatus();candidateKey=key;if(key)void readCandidateStatus();}
 renderCandidateStatus();
}
function renderCandidateStatus(){
 const active=Boolean(candidateKey);$('candidate-receipt').hidden=!active;
 let message='尚未确认候选状态，请读取后再处理。';
 if(candidateLoading)message='正在读取当前作业版本的候选状态…';
 else if(candidateError)message=candidateError;
 else if(candidateRecord)message=`候选 ${candidateRecord.id} · 作业版本 ${candidateRecord.reportVersion} · ${candidateStates[candidateRecord.status]}。`;
 else if(candidateLoaded)message='当前作业版本尚未提交案例候选。';
 $('candidate-status').textContent=message;
 controlDisabled('refresh-candidate-status',!active||candidateLoading);controlDisabled('open-candidate',!active||!candidateRecord||candidateLoading);
 const editable=!active||candidateLoaded&&!candidateRecord&&!candidateLoading&&!candidateUncertain;
 for(const id of ['candidate-text','candidate-comment','provenance'])$(id).readOnly=!editable;
 for(const id of ['candidate-feedback','reuse-confirmed'])$(id).disabled=!editable;
 controlDisabled('submit-candidate',!editable);
 if(active){const draft=['candidate-text','candidate-comment','provenance'].some(id=>$(id).value)||$('reuse-confirmed').checked||$('candidate-feedback').checked;$('candidate-form').hidden=Boolean(candidateRecord)&&!draft;}
}
async function readCandidateStatus(){
 if(!candidateKey||!job)return;const context=candidateContext(),key=candidateKey,request=++candidateRequest;
 candidateAborter?.abort();const controller=new AbortController();candidateAborter=controller;candidateLoading=true;candidateError='';renderCandidateStatus();
 const current=()=>request===candidateRequest&&key===candidateKey&&currentCandidateContext(context);
 try{const value=await candidateApi(`/jobs/${encodeURIComponent(context.id)}/candidates`,{},controller);if(!current())return;
  if(value.jobId!==context.id||value.reportVersion!==context.version||!Object.hasOwn(value,'candidate')||value.candidate!==null&&!validCandidateReceipt(value.candidate,context))throw Error('候选状态与当前作业版本不一致，请重新打开作业。');
  candidateRecord=value.candidate;candidateLoaded=true;candidateUncertain=false;
 }catch(error){if(current()){candidateLoaded=false;candidateUncertain=true;candidateError=error.message;}}
 finally{if(candidateAborter===controller){candidateAborter=null;candidateLoading=false;if(key===candidateKey)renderCandidateStatus();}}
}
$('refresh-candidate-status').addEventListener('click',()=>void readCandidateStatus());
$('open-candidate').addEventListener('click',e=>busy(e.target,async()=>{const item=candidateRecord;if(item)await openKnowledge(item.id,item.jobId);}));
$('candidate-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
 if(!job)return;if(candidateKey&&(!candidateLoaded||candidateLoading||candidateUncertain||candidateRecord))throw Error('请先读取候选状态；已有候选可在审核页继续处理。');
 const context=candidateContext(),edits=editVersion,body={version:job.version,anonymizedText:$('candidate-text').value,anonymizedComment:$('candidate-comment').value,reuseReviewFeedback:$('candidate-feedback').checked,reuseAuthorized:$('reuse-confirmed').checked,provenance:$('provenance').value};
 candidateRequest++;candidateAborter?.abort();candidateAborter=null;candidateLoading=false;candidateUncertain=true;
 try{const saved=await candidateApi(`/jobs/${encodeURIComponent(context.id)}/candidates`,{method:'POST',body});if(!currentCandidateContext(context))return;
  if(!validCandidateReceipt(saved,context))throw Error('候选保存响应尚未确认，请读取状态核对。');
  candidateKey=candidateIdentity();candidateRecord=saved;candidateLoaded=true;candidateUncertain=false;candidateError='';
  if(edits===editVersion){for(const id of ['candidate-text','candidate-comment','provenance'])$(id).value='';$('reuse-confirmed').checked=false;$('candidate-feedback').checked=false;dirty=false;}
  renderCandidateStatus();notify(`候选已保存：${candidateStates[saved.status]}。可打开候选审核查看或继续处理。`);
 }catch(error){if(!currentCandidateContext(context))return;candidateKey=candidateIdentity();candidateUncertain=true;candidateError='提交结果尚未确认，填写内容保留。请读取候选状态后再处理。';renderCandidateStatus();throw error;}
 });});

async function loadEvents(){
  if(!job)return;
  const id=job.id,selection=selectionVersion,request=++eventRequest;
  const data=await api(`/jobs/${id}/events${eventCursor?`?after=${eventCursor}`:''}`);
  if(selection!==selectionVersion||job?.id!==id||request!==eventRequest)return;
  const fresh=data.items.filter(item=>item.id>eventCursor);
  eventCount+=fresh.length;eventItems=[...eventItems,...fresh].slice(-100);
  eventCursor=Math.max(eventCursor,data.nextCursor||0,...fresh.map(item=>item.id));
  $('events').replaceChildren();
  const names={'revision.created':'修订稿已创建','revision.saved':'修订稿已保存','revision.confirmed':'修改记录已确认','job.created':'任务已创建','image.attached':'原稿已保存','text.confirmed':'原文已确认','retrieval.started':'检索适用规则与案例','scoring.started':'五维独立评分','reflection.started':'复核评分依据','revision.started':'修订争议维度','arbitration.completed':'完成冲突检查','report.approved':'教师已确认','candidate.submitted':'已提交案例候选','candidate.approved':'案例候选已通过独立审核','candidate.rejected':'案例候选已退回修改','candidate.revised':'案例候选已修订并重新提交','candidate.withdrawn':'案例候选已撤回','candidate.published':'案例候选已入库','job.cancelled':'任务已取消','job.retry_requested':'已请求恢复处理'};
  for(const item of eventItems)$('events').append(node('li',`${names[item.event]||states[item.event]||'处理状态已更新'} · ${new Date(item.createdAt).toLocaleTimeString()}`));
  controlDisabled('events-more',!data.hasMore);
  $('events-page').textContent=`已读取 ${eventCount} 条，显示最近 ${eventItems.length} 条。${data.hasMore?'还有后续记录，请继续读取。':'已读取至本次查询的最新记录。'}`;
  const stage=[...eventItems].reverse().find(item=>['retrieval.started','scoring.started','reflection.started','revision.started','arbitration.completed'].includes(item.event));
  updateProgress(data.hasMore?'处理记录还有后续。':stage?names[stage.event]:'');
}
$('events-more').addEventListener('click',()=>busy($('events-more'),loadEvents));
$('events-restart').addEventListener('click',()=>busy($('events-restart'),async()=>{resetEvents();await loadEvents();}));
function schedulePoll(attempt=0){
  stopPolling();if(!job||!token||mutationRecovery?.id===job.id||!['queued','running','ocr_queued','ocr_running'].includes(job.status))return;
  const id=job.id;
  timer=setTimeout(async()=>{
    aborter=new AbortController();
    try{
      const next=await api(`/jobs/${id}`,{signal:aborter.signal});if(job?.id!==id||mutationRecovery?.id===id)return;
      const fill=job.status==='ocr_running'||job.status==='ocr_queued';job=next;renderJob(fill);await loadEvents();schedulePoll();
    }catch(error){
      if(error.name!=='AbortError'&&job?.id===id&&token){
        notify(attempt<5?'连接暂时中断，正在重新读取处理状态。':'状态更新暂时中断，请重新选择作业以继续。');
        if(attempt<5)schedulePoll(attempt+1);
      }
    }
  },Math.min(2000*2**attempt,30000));
}

const readinessLabels={task_state:'任务状态',rubric_snapshot:'量规快照',text_binding:'确认文字绑定',report_content:'报告结构、总分与引用',report_rubric_binding:'报告量规绑定',model_identifier:'模型标识',model_revision:'模型版本',pipeline_revision:'流程版本',teacher_confirmation:'教师确认'};
const readinessReasons={REPORT_STATE_READY:'已形成可核对的报告版本。',REPORT_STATE_PENDING:'当前任务尚未形成可打印报告。',RUBRIC_RECORDED:'当前量规快照已记录。',RUBRIC_UNAVAILABLE:'量规快照缺失或无效，请核对处理记录。',TEXT_BINDING_MATCHED:'报告与当前确认文字一致。',TEXT_BINDING_UNAVAILABLE_OR_CHANGED:'文字或报告绑定缺失、已变化，请先核对。',REPORT_STRUCTURE_CHECKED:'报告结构、总分及所用原文引用已核对。',REPORT_CONTENT_UNAVAILABLE_OR_INVALID:'报告内容缺失或结构、引用不完整。',REPORT_TOTAL_MISMATCH:'模型报告总分与维度评分、量规权重不一致，请核对已保存报告。',TEACHER_TOTAL_MISMATCH:'教师终评总分与维度评分、量规权重不一致，请核对已保存报告。',RUBRIC_BINDING_CHANGED:'报告量规与当前快照不一致。',RUBRIC_BINDING_MATCHED:'报告记录了相同量规版本。',LEGACY_RUBRIC_BINDING_UNRECORDED:'历史报告缺少显式量规绑定，下载时保留待核对提示。',MODEL_RECORDED:'报告模型标识已记录。',MODEL_UNRECORDED:'缺少报告模型标识。',VERSION_RECORDED:'已记录版本信息。',VERSION_UNRECORDED:'历史记录未保存该版本，下载时保留未记录提示。',VERSION_METADATA_INVALID:'保存的版本信息无效，请核对处理记录。',TEACHER_CONFIRMED:'教师确认记录已保存。',TEACHER_REVIEW_REQUIRED:'尚待教师复核，仅可下载待复核稿。',TEACHER_CONFIRMATION_UNAVAILABLE:'任务状态与教师确认记录不完整或不一致。'};
const readinessNext={confirm_text:['text-form','前往核对原文'],wait:['events','查看处理进度'],inspect_events:['events','查看处理记录'],review_report:['report-content','前往复核报告'],read_report:['report-content','查看已保存报告'],closed:['events','查看处理记录']};
function readinessIdentity(){return job?`${selectionVersion}:${job.id}:${job.version}`:'';}
function clearReadiness(){readinessRequest++;readinessAborter?.abort();readinessAborter=null;reportReadiness=null;readinessKey='';$('readiness-checks').replaceChildren();$('readiness-status').textContent='尚未检查，暂不下载报告。';$('readiness-next').hidden=true;controlDisabled('export-job',true);}
function renderReadiness(){
 if(readinessKey&&readinessKey!==readinessIdentity())clearReadiness();
 if(!reportReadiness){controlDisabled('export-job',true);if(!readinessAborter)$('readiness-status').textContent=dirty?'有未保存的修改，请先保存或确认后再检查报告。':'当前保存版本尚未检查，请点击上方检查按钮。';return;}
 const data=reportReadiness;controlDisabled('export-job',!data.canExport||dirty);
 $('export-job').textContent=data.teacherConfirmed?'下载可打印报告':'下载待教师复核稿';
 $('readiness-status').textContent=data.state==='ready'?'本版本检查完成，可下载报告。':data.canExport?'可下载标明待复核事项的报告，请逐项核对。':'报告尚未就绪，请先处理下列事项。';
 $('readiness-checks').replaceChildren(...data.checks.map(item=>node('li',`${readinessLabels[item.code]}：${{passed:'通过',warning:'待核对',blocked:'未就绪'}[item.state]}。${readinessReasons[item.reason]}`)));
 const [target,label]=readinessNext[data.nextAction];$('readiness-next').href='#'+target;$('readiness-next').textContent=label;$('readiness-next').hidden=false;
}
function validReadiness(data){
 if(!data||data.schema!=='inkwise.report-readiness/v1'||data.jobId!==job?.id||data.jobVersion!==job?.version||typeof data.canExport!=='boolean'||typeof data.teacherConfirmed!=='boolean'||!['scoring','feedback','unavailable'].includes(data.reportKind)||!Object.hasOwn(readinessNext,data.nextAction)||!Array.isArray(data.checks)||data.checks.length>12)return false;
 if(!data.checks.every(item=>item&&Object.hasOwn(readinessLabels,item.code)&&Object.hasOwn(readinessReasons,item.reason)&&['passed','warning','blocked'].includes(item.state)))return false;
 const codes=new Set(data.checks.map(item=>item.code));
 if(codes.size!==data.checks.length||!['task_state','rubric_snapshot','text_binding','report_content','model_identifier','model_revision','pipeline_revision','teacher_confirmation'].every(code=>codes.has(code)))return false;
 const blocked=data.checks.some(item=>item.state==='blocked'),warning=data.checks.some(item=>item.state==='warning');
 return data.state===(blocked?'blocked':warning?'review':'ready')&&data.canExport===!blocked&&data.teacherConfirmed===(data.checks.find(item=>item.code==='teacher_confirmation').state==='passed');
}
async function checkReportReadiness(){
 if(!job)return;if(dirty){notify('有尚未保存的修改，请先保存或确认后再检查报告。');return;}
 clearReadiness();const request=readinessRequest,key=readinessIdentity(),controller=new AbortController();readinessAborter=controller;$('readiness-status').textContent='正在检查当前保存版本…';
 try{const data=await api(`/jobs/${job.id}/report-readiness?version=${job.version}`,{signal:controller.signal});if(request!==readinessRequest||key!==readinessIdentity())return;if(!validReadiness(data))throw Error('就绪检查返回不完整，请重新检查当前版本。');reportReadiness=data;readinessKey=key;renderReadiness();}
 catch(error){if(request===readinessRequest&&key===readinessIdentity())$('readiness-status').textContent='本次检查未完成，请重试；原报告保留。';throw error;}
 finally{if(readinessAborter===controller)readinessAborter=null;}
}
$('check-report-readiness').addEventListener('click',event=>busy(event.target,checkReportReadiness));
$('export-job').addEventListener('click',()=>busy($('export-job'),exportCurrentJob));
async function exportCurrentJob(){
  if(!job)return;
  if(dirty){notify('有尚未保存的修改，请先保存或确认后再下载报告。');return;}
  if(!reportReadiness?.canExport||readinessKey!==readinessIdentity()){notify('请先完成当前版本的报告就绪检查。');return;}
  const id=job.id,version=job.version,selection=selectionVersion;
  let exported;try{exported=await api(`/jobs/${id}/export-snapshot?version=${version}&purpose=printable`);}catch(error){if(selection===selectionVersion&&job?.id===id&&job.version===version)clearReadiness();throw error;}
  if(selection!==selectionVersion||job?.id!==id||job.version!==version||dirty)return;
  if(!validReadiness(exported.readiness)||!exported.readiness.canExport){clearReadiness();throw Error('报告就绪状态已变化，请重新检查。');}
  downloadReport(exported.job,exported.binding,exported.readiness);
}
function downloadReport(job,binding,readiness){
  const doc=document.implementation.createHTMLDocument('墨评批改报告');
  doc.documentElement.lang='zh-CN';
  const charset=doc.createElement('meta');charset.charset='utf-8';doc.head.append(charset);
  const policy=doc.createElement('meta');policy.httpEquiv='Content-Security-Policy';policy.content="default-src 'none'; style-src 'unsafe-inline'; object-src 'none'; base-uri 'none'";doc.head.append(policy);
  const style=doc.createElement('style');style.textContent='body{max-width:850px;margin:40px auto;padding:24px;font:16px/1.75 system-ui,sans-serif;color:#18181b}h1,h2{line-height:1.5}section{break-inside:avoid;margin:28px 0}pre{white-space:pre-wrap;font:inherit}blockquote{border-left:2px solid #71717a;margin:12px 0;padding-left:15px}small{color:#62626b}@media print{body{margin:0;padding:15mm}button{display:none}}';doc.head.append(style);
  const add=(tag,text,parent=doc.body)=>{const e=doc.createElement(tag);e.textContent=text;parent.append(e);return e;};
  if(readiness.state==='review'){add('p','本报告仍有教师复核或历史元数据待核对事项，不代表所有绑定信息完整。');for(const item of readiness.checks.filter(item=>item.state==='warning'))add('p',readinessLabels[item.code]+'：'+readinessReasons[item.reason]);}
  add('h1',job.payload.title);add('p',`${states[job.status]||job.status} · 文字版本 ${job.textVersion}`);
  add('small',`量规：${job.rubric?.title||'尚未确认'} / ${binding.rubricRelease||'—'}`);
  add('p',`学校：${binding.tenantId} · 班级：${binding.classroomId} · 作业：${binding.jobId} · 任务版本 ${binding.jobVersion}`);
  add('small',`原文 SHA-256：${binding.textHash} · 模型版本：${binding.modelRevision||'未记录'} · 流程版本：${binding.pipelineRevision||'未记录'} · 核对时间：${binding.exportedAt}`);
  add('p','此文件是上述版本在核对时的快照，之后的修改需重新下载。');
  add('h2','学生原文');add('pre',job.text);
  if(job.result?.kind==='feedback'){add('h2','校对反馈（无分数）');for(const finding of job.review?.findings||job.result.findings||[]){add('blockquote',finding.evidence.quote);add('p',finding.suggestion);}if(job.review)add('p',job.review.comment);else add('p','尚未由教师确认。');}
  else if(job.result){add('h2',`${job.review?'教师确认':'模型建议'} ${job.review?.total??job.result.total} / 100`);
    if(!job.review)add('p','此报告尚未由教师确认，不能作为正式成绩。');
    if(!job.review&&(job.result.warnings||[]).length)add('p','报告存在需复核事项，请结合工作台处理记录查看，不省略人工审核。');if(job.rubric?.synthetic)add('p','本报告采用合成示例量规，不属于学校正式成绩。');
    for(const score of job.result.scores){const section=add('section','');add('h2',`${labels[score.dimension]}：${job.review?.scores[score.dimension]??score.band} / 4`,section);const feedback=job.review?job.review.feedback?.[score.dimension]:(job.feedbackIssues?.[score.dimension]?null:score);if(feedback&&feedback.include!==false){add('p',feedback.reason,section);for(const evidence of feedback.evidence||[]){add('blockquote',evidence.quote,section);add('small',evidence.ruleId,section);}if(feedback.suggestion)add('p',feedback.suggestion,section);}else add('p',job.review?'本维度没有单独发布的评语，见教师总评。':'本维度的自动评语需教师重写，暂不发布。',section);}
    if(job.review){add('h2','教师评语');add('p',job.review.comment);}
  }else add('p','尚无有效评分结果。');
  add('small','本文件含作业内容，请按学校授权范围保存和分享。可使用浏览器打印功能另存为 PDF。');
  const url=URL.createObjectURL(new Blob(['<!doctype html>'+doc.documentElement.outerHTML],{type:'text/html;charset=utf-8'}));const link=node('a');link.href=url;link.download=readiness.teacherConfirmed?'inkwise-report.html':'inkwise-report-draft.html';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function openKnowledge(id='',sourceJobId=''){
 if(!discardEdits())return false;cancelJobSelection();const selection=selectionVersion,intent=selectionRequest,session=sessionGeneration;stopPolling();$('job-panel').inert=true;
 try{if(!await loadCandidates(0,id,sourceJobId))return false;if(selection!==selectionVersion||intent!==selectionRequest||session!==sessionGeneration)return false;
  selectionVersion++;dirty=false;candidateEdits.clear();clearNotice();showPanel('knowledge');const heading=$('knowledge-panel').querySelector('h2');heading.tabIndex=-1;heading.focus({preventScroll:true});heading.scrollIntoView({block:'start',behavior:'auto'});return true;
 }catch(error){if(selection===selectionVersion&&intent===selectionRequest)schedulePoll();throw error;}
 finally{if(intent===selectionRequest)$('job-panel').inert=false;}
}
$('show-knowledge').addEventListener('click',e=>busy(e.target,()=>openKnowledge()));
async function loadCandidates(offset=candidatesOffset,focusId=candidateFocusId,sourceJobId=candidateFocusJob,filters=appliedCandidateFilters){
 if(!$('knowledge-panel').hidden&&!discardEdits())return false;
 const request=++candidatesRequest,selection=selectionVersion,intent=selectionRequest,session=sessionGeneration,edits=editVersion;
 candidatesAborter?.abort();const controller=new AbortController();candidatesAborter=controller;
 try{
  let data;if(focusId){const item=await candidateApi(`/candidates/${encodeURIComponent(focusId)}`,{},controller);if(item.id!==focusId||item.jobId!==sourceJobId)throw Error('候选与来源作业不一致，请重新读取。');data={items:[item],hasMore:false};}
  else data=await candidateApi(`/candidates?limit=20&offset=${offset}&status=${encodeURIComponent(filters.status)}&classroom=${encodeURIComponent(filters.classroom)}&search=${encodeURIComponent(filters.search)}`,{},controller);
  if(request!==candidatesRequest||selection!==selectionVersion||intent!==selectionRequest||session!==sessionGeneration)return false;
  if(edits!==editVersion)throw Error('读取期间有新的未保存修改，当前内容保留，请保存后再刷新。');
  if(!Array.isArray(data.items)||data.items.length>20)throw Error('候选列表响应不完整，请重新读取。');
  candidatesOffset=offset;candidateFocusId=focusId;candidateFocusJob=sourceJobId;
  if(!focusId)appliedCandidateFilters={...filters};
  const items=data.items,root=$('knowledge-list');controlDisabled('candidates-prev',Boolean(focusId)||offset===0);controlDisabled('candidates-next',Boolean(focusId)||!data.hasMore);$('candidates-all').hidden=!focusId;
  $('candidates-page').textContent=focusId?'已定位当前候选；可刷新核对最新审核状态。':items.length?`第 ${offset+1}–${offset+items.length} 条`:'没有候选案例符合当前条件';
  root.replaceChildren(...items.map(candidateCard));candidateEdits.clear();dirty=false;
  if(!items.length)root.append(node('p','当前没有符合条件的候选案例，可清除筛选后查看。','hint'));return true;
 }finally{if(candidatesAborter===controller)candidatesAborter=null;}
}
async function changeCandidateFilters(){
 const session=sessionGeneration,pending=loadCandidates(0,'','',candidateFilterValues()),request=candidatesRequest;
 try{const loaded=await pending;if(!loaded&&session===sessionGeneration&&request===candidatesRequest)setCandidateFilters(appliedCandidateFilters);return loaded;}
 catch(error){if(session!==sessionGeneration||request!==candidatesRequest)return false;setCandidateFilters(appliedCandidateFilters);$('candidates-page').textContent='候选列表尚未更新，原内容保留，请重试。';throw error;}
}
async function nextCandidateReview(restart=false){
 if(!['reviewer','admin'].includes(me?.role)||!discardEdits())return false;
 const filters=candidateFilterValues(),scope=JSON.stringify([filters.classroom,filters.search]);
 const after=restart||scope!==candidateReviewScope?'':candidateReviewCursor;
 const query=new URLSearchParams({classroom:filters.classroom,search:filters.search});if(after)query.set('after_id',after);
 const request=++candidatesRequest,selection=selectionVersion,intent=selectionRequest,session=sessionGeneration,edits=editVersion;
 candidatesAborter?.abort();const controller=new AbortController();candidatesAborter=controller;
 const current=()=>request===candidatesRequest&&selection===selectionVersion&&intent===selectionRequest&&session===sessionGeneration&&!$('knowledge-panel').hidden;
 try{
  const data=await candidateApi('/candidate-review-queue/next?'+query,{},controller);
  if(!current())return false;
  if(edits!==editVersion||JSON.stringify(candidateFilterValues())!==JSON.stringify(filters))throw Error('读取期间有新的修改，当前内容保留，请保存或确认筛选后重试。');
  if(!data||data.order!=='oldest-created'||!Object.hasOwn(data,'item'))throw Error('待审候选响应不完整，原内容保留。');
  if(data.item===null){if(data.nextAfter!==null)throw Error('待审游标响应不完整，原内容保留。');$('candidates-page').textContent=after?'没有更后的当前版本独立待审候选；当前记录保留，可从最早待审开始。':'当前条件没有可独立审核的当前版本候选，原列表保留。';return false;}
  const item=data.item,id=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(value);
  if(!id(item.id)||!id(item.jobId)||data.nextAfter!==item.id||item.status!=='pending'||item.submittedBy===me.userId||item.sourceCurrent!==true||!Number.isSafeInteger(item.version)||item.version<1||!Number.isSafeInteger(item.reportVersion)||item.reportVersion<1||item.sourceVersion!==item.reportVersion||!['approved','published'].includes(item.sourceStatus)||typeof item.classroomId!=='string'||filters.classroom&&item.classroomId!==filters.classroom||me.role!=='admin'&&!me.classroomIds.includes(item.classroomId)||typeof item.text!=='string'||item.text.length>15000||typeof item.provenance!=='string'||item.provenance.length>1000||!item.review||typeof item.review!=='object'||Array.isArray(item.review))throw Error('候选范围或来源版本未核对，原内容保留，请刷新确认。');
  const card=candidateCard(item);
  candidateReviewCursor=item.id;candidateReviewScope=scope;candidatesOffset=0;candidateFocusId=item.id;candidateFocusJob=item.jobId;
  appliedCandidateFilters={...filters,status:'pending'};setCandidateFilters(appliedCandidateFilters);
  $('knowledge-list').replaceChildren(card);candidateEdits.clear();dirty=false;controlDisabled('candidates-prev',true);controlDisabled('candidates-next',true);$('candidates-all').hidden=false;
  $('candidates-page').textContent='已定位下一份独立待审候选。请核对来源与脱敏内容，再明确提交审核决定。';
  const heading=card.querySelector('h3');heading?.focus?.({preventScroll:true});heading?.scrollIntoView?.({block:'start',behavior:'auto'});return true;
 }catch(error){if(!current())return false;$('candidates-page').textContent='待审读取未完成，原候选与输入保留。';throw error;}
 finally{if(candidatesAborter===controller)candidatesAborter=null;}
}
$('next-candidate-review').addEventListener('click',event=>busy(event.target,()=>nextCandidateReview()));
$('restart-candidate-review').addEventListener('click',event=>busy(event.target,()=>nextCandidateReview(true)));
$('candidate-search-form').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,changeCandidateFilters);});
for(const id of ['candidate-filter','candidate-classroom'])$(id).addEventListener('change',event=>busy(event.target,changeCandidateFilters));
$('candidate-reset').addEventListener('click',event=>{setCandidateFilters(emptyCandidateFilters());busy(event.target,changeCandidateFilters);});
$('refresh-candidate-list').addEventListener('click',e=>busy(e.target,()=>loadCandidates()));
$('candidates-all').addEventListener('click',e=>busy(e.target,()=>loadCandidates(0,'','')));
function caseViewCurrent(context){return context.request===candidatesRequest&&context.selection===selectionVersion&&context.intent===selectionRequest&&context.session===sessionGeneration&&!$('knowledge-panel').hidden;}
async function savedCaseAction(item,path,body){
 const context={request:candidatesRequest,selection:selectionVersion,intent:selectionRequest,session:sessionGeneration},editedAt=candidateEdits.get(item.id);
 try{await candidateApi(path,{method:'POST',body});if(!caseViewCurrent(context))return;
  if(candidateEdits.get(item.id)===editedAt)candidateEdits.delete(item.id);dirty=candidateEdits.size>0;
  const updated=await candidateApi(`/candidates/${encodeURIComponent(item.id)}`);if(!caseViewCurrent(context))return;
  if(updated.id!==item.id||updated.jobId!==item.jobId)throw Error('候选与来源作业不一致，请刷新核对。');
  // Update only the saved card. Another candidate's draft stays mounted.
  if(!candidateEdits.has(item.id)){const card=[...$('knowledge-list').querySelectorAll('[data-candidate-id]')].find(card=>card.dataset.candidateId===item.id);card?.replaceWith(candidateCard(updated));}
  notify(`候选状态已保存：${candidateStates[updated.status]||'请刷新核对'}。`);
  if(candidateReviewCursor===item.id&&!candidateEdits.size){$('candidates-page').textContent='当前审核决定已保存，可继续下一份；不会自动跳转。';$('next-candidate-review').focus?.({preventScroll:true});}
 }catch(error){if(caseViewCurrent(context))throw error;}
}

function candidateCard(item){const card=node('article',undefined,'candidate');card.dataset.candidateId=item.id;if(item.classroomId||item.sourceTitle)card.append(node('p',[item.classroomId,item.sourceTitle].filter(Boolean).join(' · '),'hint'));const heading=node('h3',`案例 ${item.id.slice(0,8)} · ${candidateStates[item.status]||'状态待确认'}`);heading.tabIndex=-1;card.append(heading,node('pre',item.text),node('p',`来源与授权：${item.provenance||'未提供'}`),node('p',`案例评语：${item.review?.comment||'未提供'}`));const versions=node('div',undefined,'candidate-source-versions');versions.setAttribute('aria-label','来源版本');versions.append(node('strong','来源版本'),node('p','候选版本：'+item.version+' · 绑定报告版本：'+item.reportVersion),node('p','读取时作业版本：'+(Number.isSafeInteger(item.sourceVersion)?item.sourceVersion:'未核对')+' · '+(states[item.sourceStatus]||'来源状态未核对')));card.append(versions);if(item.decisionReason)card.append(node('p',`审核意见：${item.decisionReason}`,'notice'));for(const [dimension,feedback] of Object.entries(item.review?.feedback||{})){if(feedback.include){card.append(node('h4',labels[dimension]||dimension),node('p',feedback.reason),node('p',feedback.suggestion));for(const evidence of feedback.evidence||[])card.append(node('blockquote',evidence.quote));}}if(item.sourceCurrent===false)card.append(node('p','来源作业版本已变化，此候选保留为历史；请先核对当前作业。','notice'));if(item.sourceCurrent!==false&&item.submittedBy===me.userId&&['pending','rejected'].includes(item.status))card.append(candidateEditor(item));if(item.status==='pending'&&me.role!=='teacher'&&item.submittedBy!==me.userId){const controls=document.createElement('form');controls.dataset.unsaved='true';controls.dataset.candidateId=item.id;controls.addEventListener('submit',event=>event.preventDefault());const reason=document.createElement('input');reason.placeholder='审核依据';reason.setAttribute('aria-label','审核依据');controls.append(reason);for(const [decision,label] of [['approve','批准候选'],['reject','退回候选']]){const button=node('button',label);button.type='button';button.addEventListener('click',()=>busy(button,async()=>{if(!reason.value.trim())throw Error('请填写审核依据。');await savedCaseAction(item,`/candidates/${encodeURIComponent(item.id)}/decisions`,{version:item.version,decision,reason:reason.value});}));controls.append(button);}card.append(controls);}if(item.submittedBy===me.userId&&item.status!=='withdrawn'){const withdraw=node('button','撤回候选');withdraw.addEventListener('click',()=>busy(withdraw,async()=>{if(!discardEdits())return;await savedCaseAction(item,`/candidates/${encodeURIComponent(item.id)}/withdraw`,{version:item.version});}));card.append(withdraw);}return card;}

function candidateEditor(item){
  const wrapper=node('div'),toggle=node('button','修改候选'),form=document.createElement('form');
  form.hidden=true;form.dataset.unsaved='true';form.dataset.candidateId=item.id;
  const field=(label,value,maximum)=>{const row=node('label',label),input=document.createElement('textarea');input.value=value;input.maxLength=maximum;input.required=true;row.append(input);form.append(row);return input;};
  const text=field('脱敏正文',item.text,15000),comment=field('脱敏评语',item.review?.comment||'',3000),source=field('来源与授权说明',item.provenance||'',1000);
  const reuseLabel=node('label',undefined,'check'),reuse=document.createElement('input');reuse.type='checkbox';
  reuseLabel.append(reuse,node('span','已重新核对原评分的逐维评语和引用，允许用于这次脱敏案例'));form.append(reuseLabel);
  const consentLabel=node('label',undefined,'check'),consent=document.createElement('input');consent.type='checkbox';consent.required=true;
  consentLabel.append(consent,node('span','已核对本次修改的脱敏内容并取得使用授权'));form.append(consentLabel);
  const save=node('button','保存修改并重新提交审核');save.type='submit';form.append(save);
  toggle.addEventListener('click',()=>{form.hidden=false;toggle.hidden=true;});
  form.addEventListener('submit',event=>{event.preventDefault();busy(save,async()=>{
    await savedCaseAction(item,`/candidates/${encodeURIComponent(item.id)}/revisions`,{version:item.version,anonymizedText:text.value,
      anonymizedComment:comment.value,provenance:source.value,reuseReviewFeedback:reuse.checked,reuseAuthorized:consent.checked});
  });});
  wrapper.append(toggle,form);return wrapper;
}
$('candidates-prev').addEventListener('click',e=>busy(e.target,()=>loadCandidates(Math.max(0,candidatesOffset-20))));
$('candidates-next').addEventListener('click',e=>busy(e.target,()=>loadCandidates(candidatesOffset+20)));
window.addEventListener('pagehide',()=>{logout();clearTimeout(noticeTimer);});

$('zoom-original').addEventListener('click',()=>{if(imageUrl){$('large-original').src=imageUrl;$('image-dialog').showModal();}});
$('close-image-dialog').addEventListener('click',()=>$('image-dialog').close());
$('image-dialog').addEventListener('keydown',event=>{
  if(event.key!=='Tab')return;
  const dialog=$('image-dialog'),stops=[...dialog.querySelectorAll('button:not([disabled]),[tabindex="0"]')];
  const first=stops[0],last=stops.at(-1);
  if(!first)return;
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
});
$('image-dialog').addEventListener('close',()=>{$('large-original').removeAttribute('src');if(imageUrl&&!$('zoom-original').hidden)$('zoom-original').focus({preventScroll:true});});

$('library-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
  libraryAborter?.abort();const controller=new AbortController();libraryAborter=controller;const request=++libraryRequest;
  try{const data=await api(`/library?language=${encodeURIComponent($('library-language').value)}&grade=${Number($('library-grade').value)}&query=${encodeURIComponent($('library-query').value)}`,{signal:controller.signal});if(request!==libraryRequest)return;
  const root=$('library-list');root.replaceChildren();
  if(!data.items.length)root.append(node('p','该学段暂无可用参考，请由教研补充。'));
  for(const item of data.items){const card=node('article',undefined,'candidate');card.append(node('h3',item.title),node('p',item.body));
    for(const source of item.sources){const link=node('a',source.title);link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';card.append(link,node('small',source.license));}root.append(card);}
  }finally{if(libraryAborter===controller)libraryAborter=null;}
});});

$('feedback-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{
  const findings=(job.result.findings||[]).flatMap((item,index)=>$(`proof-include-${index}`).checked?[{...item,suggestion:$(`proof-suggestion-${index}`).value}]:[]);
  if(!await mutateCurrentJob('feedback-approval',{method:'POST',body:{version:job.version,comment:$('feedback-comment').value,findings,findingsConfirmed:$('proof-feedback-confirmed').checked}}))return;
  const saved=continuationIdentity();dirty=false;renderJob();await refreshJobs();savedTaskContinuation('本次教师选中的校对反馈已保存，可继续下一份。',saved);notify('教师选中的校对反馈已保存，没有生成分数。');
});});
$('explain-report').addEventListener('click',e=>busy(e.target,async()=>{
  const id=job.id,selection=selectionVersion,data=await api(`/jobs/${id}/explanation`);if(selection!==selectionVersion||job?.id!==id)return;
  const root=$('explanation');root.replaceChildren();root.append(node('h3','本次评分依据'));
  if(data.message)root.append(node('p',data.message));
  for(const item of data.dimensions){root.append(node('h4',`${labels[item.dimension]} · ${item.band} / 4`));for(const descriptor of item.descriptors)root.append(node('p',descriptor));if(item.reason)root.append(node('p',item.reason));for(const evidence of item.evidence)root.append(node('blockquote',evidence.quote));}
}));

$('detect-language').addEventListener('click',e=>busy(e.target,async()=>{
  const text=$('new-text').value,rubric=rubrics.find(item=>item.id===$('rubric').value);
  if(!text.trim())throw Error('请先填写正文。');
  const selectedRubric=$('rubric').value;
  const data=await api('/language-preview',{method:'POST',body:{text,policy:rubric?.languagePolicy||'mixed'}});
  if($('new-text').value!==text||$('rubric').value!==selectedRubric)return;
  const names={zh:'中文',en:'英文',fr:'法文',es:'西班牙文',de:'德文',ja:'日文',ko:'韩文',th:'泰文',vi:'越南文',unknown:'不确定'};
  $('language-hints').textContent='检测提示：'+[...new Set(data.spans.map(item=>names[item.language]||'不确定'))].join('、')+'。作业语言仍以你选择的量规为准，方言不会自动判定。';
}));

async function loadLearning(){
  learningAborter?.abort();const controller=new AbortController();learningAborter=controller;const request=++learningRequest;
  const data=await api('/learning',{signal:controller.signal});if(request!==learningRequest)return;
  const root=$('learning-list');root.replaceChildren();$('experiment-list').replaceChildren();
  $('run-learning').hidden=me.role!=='admin';$('experiment-form').hidden=me.role==='teacher';
  if(!data.proposals.length)root.append(node('p','暂没有改进提案。需要获授权的案例、学习者代号和固定模型版本。','hint'));
  for(const item of data.proposals){const card=node('article',undefined,'candidate');card.append(node('h3',`${item.rubricId} · ${item.cases} 个案例`),node('p',`状态：${proposalStates[item.state]||'待教研核对'}；来源：${item.sourceValid?'仍有效':'已撤回或改变'}`));
    if(item.reasons.length)card.append(node('p','尚未满足：'+item.reasons.map(code=>learningReasons[code]||'需要教研负责人核对评估记录').join('；'),'hint'));
    if(item.metrics)card.append(node('p',`留出集误差：${item.metrics.baseline.mae.toFixed(2)} → ${item.metrics.candidate.mae.toFixed(2)}；一致性：${item.metrics.candidate.qwkFiveBands}`));
    if(item.hasProposedRubric&&item.sourceValid){const note=document.createElement('input');note.placeholder='审核说明';note.setAttribute('aria-label','审核说明');card.append(note);
      const actions=item.state==='proposal-not-active'?[['submit','提交教研复核']]:item.state==='submitted'&&me.role!=='teacher'?[['approve','批准为候选（不启用）']]:item.state==='approved-inactive'&&me.role!=='teacher'?[['publish','对照通过后启用模型聚合']]:[];
      for(const [action,label] of actions){const button=node('button',label);button.addEventListener('click',()=>busy(button,async()=>{if(!note.value.trim())throw Error('请填写审核说明。');const experimentId=action==='publish'?window.prompt('填写已完成对照评测的试验编号'):null;if(action==='publish'&&!experimentId)return;await api(`/learning/proposals/${item.id}`,{method:'POST',body:{version:item.version,action,note:note.value,experimentId}});await loadLearning();}));card.append(button);}
      if(item.state==='approved-inactive'&&me.role!=='teacher'){const prepare=node('button','填写影子试验');prepare.addEventListener('click',()=>{$('experiment-rubric').value=item.rubricId;$('experiment-release').value=item.proposedRelease;$('experiment-model').value=item.modelRevision;$('experiment-classes').value=item.classrooms.join(',');$('experiment-grade').value=String(item.grade);$('experiment-form').scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});});card.append(prepare);}
    }root.append(card);
  }
  for(const item of data.experiments){const card=node('article',undefined,'candidate');card.append(node('h3',`${item.id} · ${item.mode==='shadow'?'影子':'模型建议A/B'}`),node('p',`${item.status==='paused-regression'?'因效果下降已暂停':!item.enabled?'已暂停新参与':Date.parse(item.expiresAt)<=Date.now()?'已结束':'进行中'} · ${item.classrooms.join('、')}`));
    if(me.role!=='teacher'){const evaluate=node('button','评估对照结果');evaluate.addEventListener('click',()=>busy(evaluate,async()=>{const report=await api(`/experiments/${item.id}/evaluate`,{method:'POST',body:{}});card.append(node('pre',JSON.stringify(report,null,2)));}));card.append(evaluate);const pause=node('button','暂停新参与');pause.disabled=!item.enabled;pause.addEventListener('click',()=>busy(pause,async()=>{await api(`/experiments/${item.id}/control`,{method:'POST',body:{version:item.version,enabled:false}});await loadLearning();}));card.append(pause);}
    $('experiment-list').append(card);
  }
}
$('load-learning').addEventListener('click',e=>busy(e.target,loadLearning));
$('run-learning').addEventListener('click',e=>busy(e.target,async()=>{await api('/learning/run',{method:'POST',body:{}});await loadLearning();}));
$('experiment-form').addEventListener('submit',e=>{e.preventDefault();busy(e.submitter,async()=>{await api('/experiments',{method:'POST',body:{id:$('experiment-id').value,rubricId:$('experiment-rubric').value,candidateRelease:$('experiment-release').value,grade:Number($('experiment-grade').value),modelRevision:$('experiment-model').value,classrooms:$('experiment-classes').value.split(',').map(value=>value.trim()).filter(Boolean),mode:$('experiment-mode').value}});dirty=false;await loadLearning();});});
$('withdraw-experiment').addEventListener('click',e=>busy(e.target,async()=>{
  const id=job.id,selection=selectionVersion;
  const result=await api(`/jobs/${id}/experiment-withdrawal`,{method:'POST',body:{version:job.version}});
  if(selection!==selectionVersion||job?.id!==id)return;
  job.experimentParticipation=result.experimentParticipation;e.target.hidden=true;notify('已退出后续对照统计，既有成绩保持不变。');
}));

// Only the selected manual revision lives in memory; source reports stay in job.
let revisionListLoaded=false,writingRevision=null,revisionDirty=false,revisionAborter=null,revisionRequest=0,revisionCreateKey=null,revisionBusy=false;
Object.assign(errors,{
  APPROVED_REPORT_REQUIRED:'请先完成首稿的教师确认。',
  REVISION_ASSESSMENT_REQUIRED:'修订对照支持已确认的评分报告。',
  REVISION_SOURCE_CHANGED:'首稿或教师结论已变化，请先重新核对原报告。',
  REVISION_LIMIT:'已保留 3 份修订稿，请打开已有记录。',
  REVISION_FEEDBACK_MISMATCH:'原意见清单已变化，请重新读取。',
  REVISION_REVIEW_INCOMPLETE:'请逐项记录原意见的处理情况，再确认。',
});
function clearRevisions(){
  revisionListLoaded=false;clearReassessment();
  revisionRequest++;revisionAborter?.abort();revisionAborter=null;writingRevision=null;revisionDirty=false;revisionCreateKey=null;revisionBusy=false;$('revision-panel').inert=false;
  $('revision-panel').hidden=true;$('revision-form').hidden=true;$('revision-confirmation').hidden=true;
  for(const id of ['revision-list','revision-feedback','revision-before','revision-after'])$(id).replaceChildren();
  $('revision-text').value='';$('revision-note').value='';$('revision-status').textContent='';$('revision-reviewed').checked=false;
  for(const id of ['revision-title','revision-binding','revision-length'])$(id).textContent='';
  controlDisabled('revision-new',false);controlDisabled('revision-confirm',true);$('open-revisions').hidden=true;
}
function revisionEditable(){return ['teacher','admin'].includes(me?.role)&&writingRevision?.status==='draft';}
function renderRevisionAvailability(){
  const available=revisionsAvailable();
  $('revision-panel').hidden=!available;$('open-revisions').hidden=!available;$('revision-new').hidden=!['teacher','admin'].includes(me?.role);
  controlDisabled('stage-revisions',!available);
  if(!available&&writingRevision)clearRevisions();
  if(!available&&jobStage==='revisions')jobStage=job?.result?'report':'records';
  syncJobStage();
}
function revisionConfirmationState(){
  controlDisabled('revision-confirm',!revisionEditable()||revisionDirty||!$('revision-reviewed').checked||Object.values(writingRevision?.resolutions||{}).some(value=>value==='pending'));
}
function renderWritingRevision(value){
  clearReassessment();
  writingRevision=value;revisionDirty=false;$('revision-form').hidden=false;$('revision-diff').hidden=false;
  const editable=revisionEditable(),pending=Object.values(value.resolutions).filter(state=>state==='pending').length;
  const needsWork=Object.values(value.resolutions).filter(state=>state==='needs_work').length;
  $('revision-title').textContent='修订稿 '+value.ordinal;
  $('revision-status').textContent=value.status==='confirmed'?'已复核'+(needsWork?' · '+needsWork+' 项仍待修改':''):'草稿 · '+pending+' 项原意见待核对'+(needsWork?' · '+needsWork+' 项仍待修改':'');
  $('revision-binding').textContent='对照首稿文字版 '+value.source.textVersion+'；首稿分数与教师结论保留。';
  $('revision-text').value=value.text;$('revision-text').readOnly=!editable;$('revision-note').value=value.note;$('revision-note').readOnly=!editable;
  $('revision-length').textContent=Array.from(value.text).length+' / 15,000 字符';$('revision-save').hidden=!editable;
  $('revision-before').replaceChildren();$('revision-after').replaceChildren();
  for(const part of value.diff.parts){
    if(part.before)$('revision-before').append(node(part.kind==='equal'?'span':'del',part.before,part.kind==='equal'?'':'revision-removed'));
    if(part.after)$('revision-after').append(node(part.kind==='equal'?'span':'ins',part.after,part.kind==='equal'?'':'revision-added'));
  }
  $('revision-feedback').replaceChildren();
  for(const [index,item]of value.feedback.entries()){
    const title=item.id==='overall'?'整体评语':labels[item.id]||'原意见',card=node('details'),summary=node('summary',title+' · '+({pending:'未核对',addressed:'已处理',needs_work:'仍需修改'}[value.resolutions[item.id]]||'未核对'));
    card.open=index===0;card.append(summary,node('p',item.reason));
    if(item.suggestion)card.append(node('p',item.suggestion));
    for(const evidence of item.evidence){card.append(node('blockquote',evidence.quote));if(evidence.locatable){const button=citationButton(evidence);button.textContent='定位首稿引用';card.append(button);}}
    const label=node('label',title+'的处理'),select=node('select');select.id='revision-resolution-'+item.id;select.dataset.feedbackId=item.id;label.htmlFor=select.id;
    for(const [key,text]of [['pending','未核对'],['addressed','已处理'],['needs_work','仍需修改']]){const option=node('option',text);option.value=key;select.append(option);}
    select.value=value.resolutions[item.id]||'pending';select.disabled=!editable;card.append(label,select);$('revision-feedback').append(card);
  }
  $('revision-reviewed').checked=false;$('revision-confirmation').hidden=!editable;revisionConfirmationState();
  $('revision-reassessment').hidden=value.status!=='confirmed';
  updateReassessmentAction();
}
async function refreshWritingRevisions(){
  if(!job)return;const id=job.id,selection=selectionVersion,request=++revisionRequest;
  revisionAborter?.abort();const controller=new AbortController();revisionAborter=controller;
  const data=await revisionApi(`/jobs/${id}/revisions`,{signal:controller.signal});
  if(request!==revisionRequest||selection!==selectionVersion||job?.id!==id)return;
  revisionListLoaded=true;$('revision-list').replaceChildren();
  for(const item of data.items){const button=node('button','修订稿 '+item.ordinal+' · '+(item.status==='confirmed'?'已复核':'草稿'));button.type='button';button.dataset.revisionId=item.id;button.addEventListener('click',()=>busy(button,()=>openWritingRevision(item.id)));$('revision-list').append(button);}
  controlDisabled('revision-new',data.items.length>=data.limit);
  if(!writingRevision)$('revision-status').textContent=data.items.length?'选择一份记录继续查看。':'尚无修订稿。';
}
async function openWritingRevision(id){
  if(revisionBusy)return;
  if(revisionDirty&&!window.confirm('修订稿有未保存修改，重新读取会放弃这些修改。继续吗？'))return;
  const jobId=job.id,selection=selectionVersion,request=++revisionRequest;
  revisionAborter?.abort();const controller=new AbortController();revisionAborter=controller;
  const data=await revisionApi(`/jobs/${jobId}/revisions/${id}`,{signal:controller.signal});
  if(request!==revisionRequest||selection!==selectionVersion||job?.id!==jobId)return;
  renderWritingRevision(data);
  if(data.status==='confirmed')await fetchReassessment();
}
$('revision-refresh').addEventListener('click',e=>busy(e.target,refreshWritingRevisions));
async function mutateWritingRevision(button,work){
  if(revisionBusy)return;const selection=selectionVersion;revisionBusy=true;$('revision-panel').inert=true;
  try{await busy(button,work);}finally{if(selection===selectionVersion){revisionBusy=false;$('revision-panel').inert=false;}}
}
$('revision-new').addEventListener('click',e=>mutateWritingRevision(e.target,async()=>{
  if(revisionDirty)throw Error('请先保存或重新读取当前修订稿。');
  const id=job.id,selection=selectionVersion;revisionCreateKey??=crypto.randomUUID();
  const data=await revisionApi(`/jobs/${id}/revisions`,{method:'POST',body:{version:job.version},idempotencyKey:revisionCreateKey});
  if(selection!==selectionVersion||job?.id!==id)return;
  revisionCreateKey=null;renderWritingRevision(data);await refreshWritingRevisions();
}));
$('revision-form').addEventListener('input',e=>{editVersion++;
  if(!revisionEditable())return;revisionDirty=true;$('revision-reviewed').checked=false;
  if(e.target.id==='revision-text'){
    $('revision-diff').hidden=true;
    $('revision-feedback').querySelectorAll('select').forEach(select=>{select.value='pending';});
    $('revision-length').textContent=Array.from($('revision-text').value).length+' / 15,000 字符';
  }
  if($('revision-status').textContent!=='有未保存的修改')$('revision-status').textContent='有未保存的修改';
  revisionConfirmationState();
});
$('revision-form').addEventListener('submit',e=>{e.preventDefault();mutateWritingRevision(e.submitter,async()=>{
  if(!revisionEditable())return;
  const text=$('revision-text').value;if(!text.trim()||Array.from(text).length>15000)throw Error('修订稿需为 1–15,000 字符。');
  const id=job.id,revisionId=writingRevision.id,selection=selectionVersion,resolutions={};
  $('revision-feedback').querySelectorAll('select').forEach(select=>{resolutions[select.dataset.feedbackId]=select.value;});
  const data=await revisionApi(`/jobs/${id}/revisions/${revisionId}`,{method:'PATCH',body:{version:writingRevision.version,text,resolutions,note:$('revision-note').value}});
  if(selection!==selectionVersion||job?.id!==id||writingRevision?.id!==revisionId)return;
  renderWritingRevision(data);notify('修订稿已保存。');await refreshWritingRevisions();
});});
$('revision-reload').addEventListener('click',e=>busy(e.target,()=>openWritingRevision(writingRevision.id)));
$('revision-reviewed').addEventListener('change',revisionConfirmationState);
$('revision-confirm').addEventListener('click',e=>mutateWritingRevision(e.target,async()=>{
  if(!revisionEditable()||revisionDirty||!$('revision-reviewed').checked)throw Error('请先保存，并确认已核对文字和原意见。');
  const id=job.id,revisionId=writingRevision.id,selection=selectionVersion;
  const data=await revisionApi(`/jobs/${id}/revisions/${revisionId}/confirmation`,{method:'POST',body:{version:writingRevision.version,confirmed:true}});
  if(selection!==selectionVersion||job?.id!==id||writingRevision?.id!==revisionId)return;
  renderWritingRevision(data);notify('修改记录已确认，首稿结论保留。');await refreshWritingRevisions();
  if(selection===selectionVersion&&job?.id===id&&writingRevision?.id===revisionId)await fetchReassessment();
}));

async function openRevisionWorkspace(){if(!setJobStage('revisions'))return;if(!revisionListLoaded)await refreshWritingRevisions();}
$('open-revisions').addEventListener('click',e=>busy(e.target,openRevisionWorkspace));

async function revisionApi(path,options){
  try{return await api(path,options);}catch(error){if(['NOT_FOUND','FORBIDDEN'].includes(error.code))clearRevisions();throw error;}
}

// Only one selected comparison is retained. No independent polling loop or raw
// report copy: opening the child uses the existing queue and review interface.
let revisionAssessment=null,reassessmentKey=null,reassessmentRequest=0,reassessmentAborter=null;
Object.assign(errors,{
  CONFIRMED_REVISION_REQUIRED:'请先保存并确认当前修订稿。',
  REASSESSMENT_INPUT_FROZEN:'复评已固定正文与量规；请回到首稿另存修订版本。',
  REASSESSMENT_TEXT_UNCHANGED:'修订稿文字与首稿相同，可直接复用原评阅，无需再评分。',
  REASSESSMENT_CHAIN_LIMIT:'请回到首稿的修订记录继续，复评任务不再派生复评。',
  REASSESSMENT_BINDING_CHANGED:'复评的正文、量规或来源绑定变化，请查看处理记录。',
  REASSESSMENT_SOURCE_UNAVAILABLE:'复评来源已不可用，未继续处理。',
  REASSESSMENT_BASELINE_INVALID:'首稿分项结论不完整，请先核对原报告。',
});
function clearReassessment(){
  reassessmentRequest++;reassessmentAborter?.abort();reassessmentAborter=null;
  revisionAssessment=null;reassessmentKey=null;
  $('revision-reassessment').hidden=true;$('reassessment-open').hidden=true;
  $('reassessment-comparison').replaceChildren();$('reassessment-status').textContent='尚未读取复评状态。';
  $('reassessment-confirm').checked=false;controlDisabled('reassessment-submit',true);
}
function updateReassessmentAction(){
  const eligible=['teacher','admin'].includes(me?.role)&&writingRevision?.status==='confirmed'&&!revisionDirty&&!job?.payload?.revisionAssessment;
  const canRequest=eligible&&revisionAssessment?.status==='not_requested';
  $('reassessment-consent').hidden=!canRequest;
  controlDisabled('reassessment-submit',!canRequest||!!revisionAssessment?.job||!$('reassessment-confirm').checked);
}
function renderReassessment(data){
  if(data?.schema!=='inkwise.revision-assessment/v1'||data.revisionId!==writingRevision?.id||data.revisionVersion!==writingRevision?.version||!['not_requested','queued','running','failed','cancelled','teacher_review','reviewed','incomplete','erased'].includes(data.status))throw Error('复评状态未确认，请重新读取。');
  if(data.after&&data.after.teacherConfirmed!==(data.status==='reviewed')||data.delta&&data.status!=='reviewed')throw Error('复评确认状态不一致，请重新读取。');
  revisionAssessment=data;$('reassessment-open').hidden=!data.job||data.job.status==='erased';
  const names={not_requested:'尚未提交独立复评。',queued:'复评已进入队列。',running:'复评正在处理。',failed:'复评未完成：'+(errors[data.job?.errorCode]||data.job?.errorCode||'原因待核对')+'。请打开独立任务检查原因，再手动恢复。',cancelled:'复评已取消；不会自动重评，可打开任务核对。',teacher_review:'模型复评已保存，等待教师确认。',reviewed:'复评已由教师确认。',incomplete:'复评依据尚不完整，请打开任务核对。',erased:'复评内容已清除。'};
  $('reassessment-status').textContent=names[data.status];$('reassessment-comparison').replaceChildren();
  if(data.after){
    const table=node('table'),head=node('tr');for(const text of ['维度','首稿教师终评',data.after.teacherConfirmed?'修订教师终评':'修订模型初评','差值'])head.append(node('th',text));
    const thead=node('thead');thead.append(head);table.append(thead);const body=node('tbody');
    for(const dimension of Object.keys(labels)){
      const before=data.before?.scores?.[dimension],after=data.after.scores?.[dimension],delta=data.delta?.scores?.[dimension];
      if(!Number.isInteger(before)||before<0||before>4||!Number.isInteger(after)||after<0||after>4||delta!==undefined&&(!Number.isInteger(delta)||delta< -4||delta>4))throw Error('复评分项未确认，请重新读取。');
      const row=node('tr');for(const text of [labels[dimension],String(before),String(after),delta===undefined?'待教师确认':(delta>0?'+':'')+delta])row.append(node('td',text));body.append(row);
    }
    table.append(body);$('reassessment-comparison').append(table,node('p','差值只比较两次保存的教师结论，不直接证明教学效果或模型准确率。','hint'));
    if(data.comparison?.modelContext!=='same'||data.comparison?.aggregationContextChanged)$('reassessment-comparison').append(node('p','模型、流程或聚合条件不同或未完整记录；请结合原文和教师依据阅读。','hint'));
  }
  updateReassessmentAction();
}
async function fetchReassessment(options={}){
  if(!job||!writingRevision)return;
  const jobId=job.id,revisionId=writingRevision.id,selection=selectionVersion,request=++reassessmentRequest;
  revisionAssessment=null;updateReassessmentAction();$('reassessment-open').hidden=true;$('reassessment-comparison').replaceChildren();$('reassessment-status').textContent=options.method==='POST'?'正在提交已确认的复评请求…':'正在读取已保存的复评状态…';
  reassessmentAborter?.abort();const controller=new AbortController();reassessmentAborter=controller;
  let data;try{data=await revisionApi(`/jobs/${jobId}/revisions/${revisionId}/reassessment`,{...options,signal:controller.signal});}catch(error){if(error.name!=='AbortError'&&selection===selectionVersion&&job?.id===jobId&&writingRevision?.id===revisionId)$('reassessment-status').textContent='复评状态未能读取，请手动刷新核对。不会自动提交或重评。';throw error;}finally{if(reassessmentAborter===controller)reassessmentAborter=null;}
  if(request!==reassessmentRequest||selection!==selectionVersion||job?.id!==jobId||writingRevision?.id!==revisionId)return;
  renderReassessment(data);if(options.method==='POST')reassessmentKey=null;
}
$('reassessment-confirm').addEventListener('change',updateReassessmentAction);
$('reassessment-refresh').addEventListener('click',e=>busy(e.target,()=>fetchReassessment()));
$('reassessment-submit').addEventListener('click',e=>mutateWritingRevision(e.target,async()=>{
  if(writingRevision?.status!=='confirmed'||revisionDirty||!$('reassessment-confirm').checked)throw Error('请先确认已保存的修订稿及本次提交。');
  reassessmentKey??=crypto.randomUUID();
  await fetchReassessment({method:'POST',body:{version:writingRevision.version,confirmed:true},idempotencyKey:reassessmentKey});
}));
$('reassessment-open').addEventListener('click',e=>busy(e.target,async()=>{const id=revisionAssessment?.job?.id;if(id)await selectJob(id);}));

$('readiness-next').addEventListener('click',event=>{event.preventDefault();const next=reportReadiness&&readinessNext[reportReadiness.nextAction];if(next)focusJobTarget(next[0]);});
