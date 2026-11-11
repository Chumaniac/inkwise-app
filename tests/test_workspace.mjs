import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

function workspace(fetch) {
  const elements=new Map(), timers=new Map(), windowListeners=new Map(); let sequence=0;
  const element=id=>{
    if(elements.has(id))return elements.get(id);
    const value={value:'',options:[],children:[],hidden:id==='workspace',textContent:'',disabled:false,open:false,listeners:{},dataset:{},
      addEventListener(name,fn){this.listeners[name]=fn;},replaceChildren(...children){this.children=children;},append(...children){this.children.push(...children);},removeAttribute(){},setAttribute(){},
      closest(){return element(id+'-parent');},querySelector(){return element(id+'-child');},querySelectorAll(){return [];},
      setCustomValidity(value){this.validationMessage=value;},reportValidity(){this.validityReported=true;return !this.validationMessage;},
      close(){this.open=false;},focus(){this.focused=true;},scrollIntoView(){this.scrolled=true;}};
    elements.set(id,value);return value;
  };
  const context=vm.createContext({document:{getElementById:element,createElement:tag=>Object.assign(element('new-'+sequence++),{tagName:tag.toUpperCase()})},
    crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000001'},
    window:{addEventListener(name,fn){windowListeners.set(name,fn);},confirm(){return true;}},fetch,FormData,AbortController,URLSearchParams,URL,Blob,console,
    setTimeout(fn,delay){const id=++sequence;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);}});
  vm.runInContext(readFileSync(new URL('../web/workspace.js',import.meta.url),'utf8'),context);
  return {context,element,timers,windowListeners,run:code=>vm.runInContext(code,context)};
}
const response=value=>({ok:true,status:200,json:async()=>value});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
{
  const app=workspace(async()=>response({}));
  app.run("me={role:'teacher'};job={id:'source',status:'approved',payload:{},text:'Original',textVersion:1,review:{scores:{grammar:3}},result:{scores:[]}};");
  app.run(`renderWritingRevision({id:'revision',ordinal:1,version:3,status:'confirmed',source:{textVersion:1},text:'Revised',note:'',resolutions:{},feedback:[],diff:{parts:[]}})`);
  assert.equal(app.element('revision-reassessment').hidden,false);
  assert.equal(app.element('reassessment-submit').disabled,true);
  app.element('reassessment-confirm').checked=true;app.run('updateReassessmentAction()');assert.equal(app.element('reassessment-submit').disabled,true,'unknown reassessment state cannot enable another request');
  app.run("renderReassessment({schema:'inkwise.revision-assessment/v1',revisionId:'revision',revisionVersion:3,status:'not_requested',job:null,after:null,delta:null})");assert.equal(app.element('reassessment-submit').disabled,false);
  const scores={content:3,coherence:3,expression:3,vocabulary:3,grammar:3};
  app.context.assessmentFixture={schema:'inkwise.revision-assessment/v1',revisionId:'revision',revisionVersion:3,status:'teacher_review',job:{id:'child',status:'teacher_review'},before:{scores,total:75},after:{scores,total:75,teacherConfirmed:false},delta:null,comparison:{modelContext:'changed',causalImprovement:false}};
  app.run('renderReassessment(assessmentFixture)');
  const rows=app.element('reassessment-comparison').children[0].children[1].children;
  assert.equal(rows.length,5);assert.equal(rows[0].children[3].textContent,'待教师确认');
  assert.equal(app.element('reassessment-submit').disabled,true);
  app.context.assessmentFixture={...app.context.assessmentFixture,status:'reviewed',after:{scores:{...scores,grammar:4},teacherConfirmed:true},delta:{scores:{content:0,coherence:0,expression:0,vocabulary:0,grammar:1},total:5}};
  app.run('renderReassessment(assessmentFixture)');
  assert.equal(app.element('reassessment-comparison').children[0].children[1].children[4].children[3].textContent,'+1');
  for(const status of ['failed','cancelled','queued']){
    app.context.assessmentState={...app.context.assessmentFixture,status,job:{id:'child',status,errorCode:'MODEL_NOT_CONFIGURED'},after:null,delta:null};app.run('renderReassessment(assessmentState)');
    assert.equal(app.element('reassessment-comparison').children.length,0);assert.equal(app.element('reassessment-open').hidden,false);
    assert.ok(app.element('reassessment-status').textContent.includes(status==='failed'?'请打开':status==='cancelled'?'已取消':'已进入队列'));
  }
  app.run('logout()');assert.equal(app.element('reassessment-comparison').children.length,0);assert.equal(app.run('revisionAssessment'),null);
  console.log('PASS: reassessment consent, draft/final separation, bounded comparison and logout cleanup');
}
const readyFixture=(id='a',version=2)=>({schema:'inkwise.report-readiness/v1',jobId:id,jobVersion:version,state:'ready',canExport:true,teacherConfirmed:true,reportKind:'scoring',nextAction:'read_report',checks:Object.entries({task_state:'REPORT_STATE_READY',rubric_snapshot:'RUBRIC_RECORDED',text_binding:'TEXT_BINDING_MATCHED',report_content:'REPORT_STRUCTURE_CHECKED',report_rubric_binding:'RUBRIC_BINDING_MATCHED',model_identifier:'MODEL_RECORDED',model_revision:'VERSION_RECORDED',pipeline_revision:'VERSION_RECORDED',teacher_confirmation:'TEACHER_CONFIRMED'}).map(([code,reason])=>({code,state:'passed',reason}))});
function allowExport(app){app.context.readyFixture=readyFixture(app.run('job.id'),app.run('job.version'));app.run('reportReadiness=readyFixture;readinessKey=readinessIdentity()');}


{
  const app=workspace(async()=>response({}));
  app.run("me={role:'teacher'};job={id:'source',status:'approved',text:'Original',textVersion:1,review:{scores:{grammar:3}},result:{scores:[]}};");
  app.run(`renderWritingRevision({id:'revision',ordinal:1,version:1,status:'draft',source:{textVersion:1},text:'<img src=x onerror=alert(1)>',note:'',resolutions:{overall:'pending'},feedback:[{id:'overall',reason:'Original teacher comment',suggestion:'',evidence:[]}],diff:{parts:[{kind:'replace',before:'Original',after:'<img src=x onerror=alert(1)>'}]}})`);
  assert.equal(app.element('revision-after').children[0].tagName,'INS');
  assert.equal(app.element('revision-after').children[0].textContent,'<img src=x onerror=alert(1)>','revision text must remain literal');
  assert.equal(app.element('revision-text').readOnly,false);
  app.element('revision-form').listeners.input({target:{id:'revision-text'}});
  assert.equal(app.run('revisionDirty'),true);assert.equal(app.run('dirty'),false,'revision editing must not consume other form state');
  app.context.window.confirm=()=>false;
  assert.equal(app.run('discardEdits()'),false,'unsaved revision must protect navigation');
  assert.equal(app.element('revision-diff').hidden,true,'saved diff cannot describe unsaved text');
  assert.equal(app.run('job.text'),'Original');
  app.run('logout()');
  assert.equal(app.element('revision-text').value,'');assert.equal(app.run('writingRevision'),null);assert.equal(app.run('revisionDirty'),false);
}
{
  let complete;
  const app=workspace(()=>new Promise(resolve=>complete=resolve));
  app.run("token='synthetic';me={role:'teacher'};job={id:'first'};");
  const read=app.run("openWritingRevision('one')");
  app.run("selectionVersion++;job={id:'second'};clearRevisions();");
  complete(response({id:'one',text:'Must not render into another job'}));await read;
  assert.equal(app.run('writingRevision'),null);assert.equal(app.element('revision-text').value,'');
}
console.log('PASS: revision text stays literal, unsaved changes are protected and stale replies clear with the selected job');

{
  const app=workspace(async()=>response({}));
  app.run(`job={status:'teacher_review',result:{total:75,warnings:['SYNTHETIC_RUBRIC_NOT_SCHOOL_STANDARD'],scores:['content','coherence','expression','vocabulary','grammar'].map((dimension,index)=>({dimension,band:3,reason:'Synthetic reason',suggestion:'Synthetic suggestion',evidence:index===4?[]:[{quote:'<img src=x> literal quote',ruleId:'synthetic-rule'}]}))}};renderReport()`);
  const root=app.element('report-content');
  const dimensions=root.children.filter(child=>child.tagName==='DETAILS');
  assert.equal(dimensions.length,5,'all dimensions must remain available');
  assert.equal(dimensions[0].open,true,'first dimension gives an initial reading point');
  assert.equal(dimensions[1].open,false,'other cited dimensions begin compact');
  assert.equal(dimensions[4].open,true,'a dimension without a quote stays visible for checking');
  assert.ok(root.children.some(child=>child.textContent.includes('不代表学校正式标准')),'report warnings must stay outside collapsed sections');
  const quote=dimensions[0].children[1].children.find(child=>child.tagName==='BLOCKQUOTE');
  assert.equal(quote.textContent,'“<img src=x> literal quote”','quote markup remains literal text');
  const before=app.element('review-scores').children.length;
  dimensions[1].open=true;
  assert.equal(app.element('review-scores').children.length,before,'reading evidence must not rebuild teacher fields');
  app.run("job={status:'teacher_review',result:{kind:'feedback',findings:[{evidence:{quote:'Original'},suggestion:'Check this'}]}};renderReport()");
  assert.equal(root.children.filter(child=>child.tagName==='DETAILS').length,0,'proofreading remains separate from scoring');
}
console.log('PASS: compact report keeps warnings, literal evidence and teacher review fields');

{
  const app=workspace(async()=>response({}));
  app.run(`job={status:'teacher_review',feedbackIssues:{grammar:'NO_CHANGE_CORRECTION'},result:{total:75,warnings:['RETRIEVAL_A','RETRIEVAL_B'],scores:['content','coherence','expression','vocabulary','grammar'].map(dimension=>({dimension,band:3,reason:dimension==='grammar'?"The verb 'polished' should be 'polished'.":'Synthetic reason',suggestion:'Synthetic suggestion',evidence:[{quote:'Synthetic',ruleId:'synthetic-rule'}]}))}};renderReport()`);
  const root=app.element('report-content');
  assert.equal(root.children.filter(child=>child.textContent==='部分参考检索不可用或已被过滤，评分依据保留当前量规。').length,1,'equivalent retrieval notices appear once');
  const descendants = element => [element, ...element.children.flatMap(descendants)];
  const controls = descendants(app.element('review-scores').children.at(-1));
  assert.equal(controls.find(child=>child.id==='include-grammar').checked,false,'no-change feedback is not selected for publication');
  assert.equal(controls.find(child=>child.id==='reason-grammar').value,'','teacher rewrites an empty field instead of publishing the invalid claim');
  assert.equal(app.run('job.result.scores[4].band'),3,'feedback filtering never changes the saved score');
  const grammar=root.children.filter(child=>child.tagName==='DETAILS').at(-1);
  assert.ok(grammar.children[1].children.some(child=>child.textContent.includes('请教师重写')));
  assert.ok(!grammar.children[1].children.some(child=>child.textContent.includes("should be 'polished'")));
}
console.log('PASS: no-change feedback stays unpublished and equivalent notices are deduplicated');

{
  const app=workspace(async()=>response({}));
  app.run("job={status:'running'};updateProgress('正在核对评分依据')");
  assert.equal(app.element('job-progress').hidden,false);
  assert.ok(app.element('job-progress').textContent.includes('正在核对评分依据'));
  assert.ok(app.element('job-progress').textContent.includes('关闭页面不会取消'));
  app.run("job.status='teacher_review';updateProgress()");
  assert.equal(app.element('job-progress').hidden,true);
  assert.equal(app.element('job-progress').textContent,'','completed work must not retain a running message');
}
console.log('PASS: long-task guidance follows the current job state');

{
  const app=workspace(async path=>{if(path.endsWith('/me'))return response({tenantId:'test',role:'teacher',classroomIds:['c']});throw Error('temporary outage');});
  app.element('token').value='synthetic-only';
  app.element('login-form').listeners.submit({preventDefault(){},submitter:app.element('submit')});
  await flush();
  assert.equal(app.element('login-panel').hidden,false,'failed login initialization must remain retryable');
  assert.equal(app.element('workspace').hidden,true,'partial session must not expose workspace');
  assert.equal(app.run('token'),'','failed initialization must clear credentials');
}
{
  const app=workspace(async()=>{throw Error('offline');});
  app.run("token='synthetic';job={id:'a',status:'running'};schedulePoll()");
  const [id,timer]=[...app.timers].find(([,timer])=>timer.delay===2000);app.timers.delete(id);
  await timer.fn();await flush();
  assert.ok([...app.timers.values()].some(timer=>timer.delay===4000),'poll should retry with bounded backoff');
  app.run('logout()');
  assert.ok(![...app.timers.values()].some(timer=>timer.delay===4000),'logout must cancel retry');
}
console.log('PASS: workspace partial-login cleanup and transient polling recovery');

{
  let first;
  const job=id=>({id,version:1,payload:{title:id,grade:7},classroomId:'c',status:'needs_text_review',text:'Synthetic',textVersion:1});
  const app=workspace(path=>{
    if(path==='/api/v1/jobs/a')return new Promise(resolve=>first=resolve);
    if(path.endsWith('/events'))return Promise.resolve(response({items:[]}));
    return Promise.resolve(response(job('b')));
  });
  app.run("token='synthetic'");
  const a=app.run("selectJob('a')"),b=app.run("selectJob('b')");await b;
  first(response(job('a')));await a;
  assert.equal(app.run('job.id'),'b','late selection response must not replace the current job');
  assert.equal(app.element('job-title').focused,true,'selected job must receive keyboard focus without changing data');
}
{
  const app=workspace(async()=>response({}));
  app.run("rubrics=[{id:'advanced',languagePolicy:'en',genre:'argumentative',gradeMin:10,gradeMax:12},{id:'middle',languagePolicy:'en',genre:'short-text',gradeMin:7,gradeMax:9}]");
  app.element('sample-text').listeners.click();
  assert.equal(app.element('rubric').value,'middle');
  assert.equal(app.element('grade').value,'7');
}
console.log('PASS: latest job selection and sample/grade alignment');

{
  let complete;
  const app=workspace(()=>new Promise(resolve=>complete=resolve));
  app.run("token='synthetic';job={id:'a',version:1}");
  const pending=app.run("mutateCurrentJob('cancel',{method:'POST',body:{version:1}})");
  app.run("selectionVersion++;job={id:'b',version:1}");
  complete(response({id:'a',version:2,status:'cancelled'}));
  assert.equal(await pending,false);
  assert.equal(app.run('job.id'),'b','late mutation result must not replace another selected job');
}
console.log('PASS: late job mutation response is isolated');

{
  const app=workspace(async()=>response({}));
  app.run("token='synthetic';me={tenantId:'a'};job={id:'a'}");
  app.element('confirmed-text').value='Synthetic private text';
  app.windowListeners.get('pagehide')();
  assert.equal(app.element('confirmed-text').value,'');
  assert.equal(app.run('job'),null);
  assert.equal(app.run('token'),'');
}
console.log('PASS: page navigation clears private session and rendered text');

{
  const app=workspace(async()=>response({id:'candidate',jobId:'a',reportVersion:1,status:'pending',version:1}));
  app.run("token='synthetic';job={id:'a',version:1};dirty=true");
  app.element('candidate-form').listeners.submit({preventDefault(){},submitter:app.element('submit')});
  await flush();
  assert.equal(app.run('dirty'),false,'saving a candidate must clear its unsaved state');
}
console.log('PASS: saved candidate has no stale unsaved state');

{
  const app=workspace(async()=>{throw Error('temporary unavailable');});
  app.run("token='synthetic';job={id:'old'};dirty=true");
  await app.run("selectJob('new')").catch(()=>{});
  assert.equal(app.run('dirty'),true,'failed navigation must keep unsaved protection');
}
{
  const app=workspace(async()=>response({}));
  app.element('sample-text').listeners.click();
  assert.equal(app.run('dirty'),true,'programmatic sample fill must mark the draft');
  app.run("dirty=true;job={id:'old',payload:{title:'Synthetic'},text:'saved'}");
  app.element('export-job').listeners.click();
  assert.match(app.element('message').textContent,/保存/,'export must not silently use saved data while edits are unsaved');
}
{
  const app=workspace(async()=>response({}));
  app.element('job-panel').hidden=false;app.element('create-panel').hidden=true;
  app.element('job-title').textContent='PREVIOUS_PRIVATE_TITLE';app.element('image-file').value='previous.png';
  app.run('logout()');
  assert.equal(app.element('job-panel').hidden,true);
  assert.equal(app.element('create-panel').hidden,false);
  assert.equal(app.element('job-title').textContent,'');
  assert.equal(app.element('image-file').value,'');
}
console.log('PASS: unsaved navigation/export, programmatic edits and account reset');

{
  const app=workspace(async()=>{throw Error('temporary candidates outage');});
  app.run("token='synthetic';job={id:'current',status:'teacher_review'};dirty=true");
  app.element('job-panel').hidden=false;app.element('knowledge-panel').hidden=true;
  app.element('show-knowledge').listeners.click({target:app.element('show-knowledge')});await flush();
  assert.equal(app.run('dirty'),true,'failed knowledge navigation must preserve pending edits');
  assert.equal(app.element('job-panel').hidden,false,'failed navigation must retain the original panel');
}

{
  const app=workspace(async()=>response({items:[],hasMore:false}));
  app.run("token='synthetic'");
  await app.run("busy($('jobs-next'),()=>refreshJobs(30))");
  assert.equal(app.element('jobs-next').disabled,true,'finished requests must preserve last-page disabled state');
}

{
  const app=workspace(async path=>{
    if(path.endsWith('/events'))throw Error('temporary audit outage');
    return response({id:'running',status:'running',payload:{title:'Synthetic',grade:7},text:'Synthetic',textVersion:1,classroomId:'c'});
  });
  app.run("token='synthetic'");
  await app.run("selectJob('running')").catch(()=>{});
  assert.ok([...app.timers.values()].some(timer=>timer.delay===2000),'event-history failure must not stop job polling');
}

{
  const requests=[];
  const app=workspace(async path=>{
    requests.push(path);
    const after=Number(new URL('http://local'+path).searchParams.get('after')||0);
    const last=Math.min(205,after+100);
    return response({items:Array.from({length:last-after},(_,i)=>({id:after+i+1,event:'scoring.started',createdAt:'2026-01-01T00:00:00Z'})),nextCursor:last,hasMore:last<205});
  });
  app.run("token='synthetic';job={id:'a',status:'running'}");
  await app.run('loadEvents()');await app.run('loadEvents()');await app.run('loadEvents()');
  assert.equal(app.run('eventCursor'),205);assert.equal(app.run('eventCount'),205);
  assert.equal(app.run('eventItems.length'),100,'display retention stays bounded');
  assert.equal(app.element('events-more').disabled,true);
  assert.ok(requests[2].endsWith('after=200'));
  app.run('logout()');assert.equal(app.run('eventItems.length'),0);
}
{
  let resolve;
  const app=workspace(()=>new Promise(done=>resolve=done));
  app.run("token='synthetic';job={id:'a',status:'running'}");
  const pending=app.run('loadEvents()');
  app.run("selectionVersion++;resetEvents();job={id:'b',status:'running'}");
  resolve(response({items:[{id:99,event:'scoring.started',createdAt:'2026-01-01T00:00:00Z'}],nextCursor:99}));await pending;
  assert.equal(app.run('eventCursor'),0,'late history must not leak into another selected job');
}
console.log('PASS: event cursor progresses beyond 100 with bounded display and selection isolation');
{
  let downloaded;
  const app=workspace(async path=>{
    assert.ok(path.endsWith('/export-snapshot?version=2&purpose=printable'));
    return response({job:{id:'a',version:2,text:'server-confirmed'},binding:{jobVersion:2,textHash:'server-hash'},readiness:readyFixture()});
  });
  app.context.captureExport=(job,binding)=>downloaded={job,binding};
  app.run("token='synthetic';job={id:'a',version:2,text:'stale-local'};downloadReport=captureExport");
  allowExport(app);await app.run('exportCurrentJob()');
  assert.equal(downloaded.job.text,'server-confirmed');assert.equal(downloaded.binding.textHash,'server-hash');
}
{
  let done,downloads=0;
  const app=workspace(()=>new Promise(resolve=>done=resolve));
  app.context.captureExport=()=>downloads++;
  app.run("token='synthetic';job={id:'a',version:2};downloadReport=captureExport");
  allowExport(app);const pending=app.run('exportCurrentJob()');app.run("selectionVersion++;job={id:'b',version:1}");
  done(response({job:{id:'a',version:2},binding:{jobVersion:2}}));await pending;
  assert.equal(downloads,0,'late exports must not download another job');
}
{
  let downloads=0;
  const app=workspace(async()=>({ok:false,status:409,json:async()=>({error:{code:'VERSION_CONFLICT'}})}));
  app.context.captureExport=()=>downloads++;
  app.run("token='synthetic';job={id:'a',version:1};downloadReport=captureExport");
  allowExport(app);await assert.rejects(app.run('exportCurrentJob()'));
  assert.equal(downloads,0,'stale reviewed reports must never be downloaded');
}
console.log('PASS: exports bind a current server snapshot and reject stale or late results');

{
  const app=workspace(async()=>response({}));
  const options=[{value:'',textContent:'全部状态（38）'}, {value:'teacher_review',textContent:'待教师复核（2）'}];
  app.element('job-filter').options=options;
  app.run("token='synthetic';logout()");
  assert.deepEqual(options.map(option=>option.textContent),['全部状态','待教师复核'],'logout must remove previous-account status counts even from hidden native options');
}
console.log('PASS: logout clears previous-account queue counts');

{
  const requests=[];
  const app=workspace(async(path,options)=>{
    requests.push({path,signal:options.signal});
    return response({items:[],hasMore:false,statusCounts:{needs_text_review:2,teacher_review:3,failed:1,running:4},actionRequiredCount:6});
  });
  const choices=[{value:'',textContent:'全部任务'},{value:'needs_action',textContent:'需要处理'}];
  app.element('job-attention').options=choices;app.element('job-attention').value='needs_action';
  app.run("token='synthetic'");await app.run('refreshJobs(30)');
  const query=new URL('http://local'+requests[0].path).searchParams;
  assert.equal(query.get('view'),'summary');assert.equal(query.get('attention'),'needs_action');
  assert.equal(choices[1].textContent,'需要处理（6）');
  app.element('job-attention').listeners.change({target:app.element('job-attention')});await flush();
  assert.equal(new URL('http://local'+requests[1].path).searchParams.get('offset'),'0');
  app.run('logout()');
  assert.equal(app.element('job-attention').value,'');
  assert.deepEqual(choices.map(option=>option.textContent),['全部任务','需要处理']);
}
{
  let resolveFirst,firstSignal;
  const app=workspace((path,options)=>{
    if(!resolveFirst)return new Promise(resolve=>{resolveFirst=resolve;firstSignal=options.signal;});
    return Promise.resolve(response({items:[],hasMore:false,statusCounts:{failed:2},actionRequiredCount:2}));
  });
  app.run("token='synthetic'");const first=app.run('refreshJobs(0)');
  await app.run('refreshJobs(30)');assert.equal(firstSignal.aborted,true);
  resolveFirst(response({items:[],hasMore:true,statusCounts:{failed:999},actionRequiredCount:999}));await first;
  assert.equal(app.run('jobsOffset'),30,'an aborted late list must not restore an older cursor');
}
{
  let resolve,signal;
  const app=workspace((path,options)=>new Promise(done=>{resolve=done;signal=options.signal;}));
  app.run("token='synthetic'");const pending=app.run('refreshJobs(0)');app.run('logout()');
  assert.equal(signal.aborted,true,'logout aborts pending queue reads');
  resolve(response({items:[],statusCounts:{failed:99},actionRequiredCount:99}));await pending;
  assert.equal(app.element('identity').textContent,'教师工作台');
}
{
  const app=workspace(async()=>response({items:null,statusCounts:{}}));
  app.run("token='synthetic'");await assert.rejects(app.run('refreshJobs(0)'));
  assert.ok(app.element('jobs-page').textContent.includes('尚未更新'));
  assert.equal(app.element('jobs-next').disabled,true,'an incomplete list does not enable a next page');
}
console.log('PASS: action queue uses summaries, aborts superseded reads and clears account metadata');


{
  let requests=0;const app=workspace(async()=>{requests++;return response({});});
  app.element('workspace').hidden=false;app.element('job-panel').hidden=false;
  app.run("me={role:'teacher'};job={status:'needs_text_review'};renderNextAction()");
  assert.ok(app.element('job-next-title').textContent.includes('原文'));
  app.element('confirmed-text').value='Unsaved synthetic teacher text';
  app.element('job-next-action').listeners.click();
  assert.equal(app.element('text-form').focused,true);
  assert.equal(app.element('confirmed-text').value,'Unsaved synthetic teacher text');
  app.run("job={status:'teacher_review',result:{kind:'feedback'}};renderNextAction()");
  assert.ok(app.element('job-next-title').textContent.includes('校对'));
  app.element('job-next-action').listeners.click();assert.equal(app.element('feedback-form').focused,true);
  app.run("job={status:'failed',errorCode:'PROVIDER_TIMEOUT_UNKNOWN'};renderNextAction()");
  app.element('job-next-action').listeners.click();assert.equal(app.element('job-error').focused,true);
  assert.equal(app.run('job.status'),'failed');assert.equal(requests,0,'navigation must not submit, retry or score');
  app.run("job={status:'needs_text_review'};me={role:'reviewer'};renderNextAction()");
  assert.ok(app.element('job-next-title').textContent.includes('任课教师'));
  app.element('job-next-action').listeners.click();assert.equal(app.element('events').focused,true);
  app.run('logout()');assert.equal(app.element('job-next').hidden,true);assert.equal(app.element('job-next-title').textContent,'');
}
console.log('PASS: next-action navigation follows role/state, preserves unsaved text and never submits work');

{
  let complete;
  const binding={schema:'inkwise.task-handoff/v1',jobId:'handoff-a',tenantId:'school',classroomId:'c',jobVersion:1,textVersion:1,status:'needs_text_review',rubricRelease:null,modelRevision:null,pipelineRevision:null,errorCode:null,lastEvent:null,nextAction:'confirm_text',instruction:'核对原文',updatedAt:'2026-10-20',generatedAt:'2026-10-20'};
  const paths=[];
  const app=workspace((path,options)=>{paths.push([path,options.method]);return new Promise(resolve=>complete=()=>resolve(response(binding)));});
  app.run("token='synthetic';job={id:'handoff-a',version:1};dirty=true");
  await app.run('generateHandoff()');assert.equal(paths.length,0,'unsaved edits block a misleading handoff');
  app.run('dirty=false');const pending=app.run('generateHandoff()');app.run("selectionVersion++;job={id:'handoff-b',version:1};clearHandoff()");complete();await pending;
  assert.equal(app.run('handoff'),null,'late response must not cross task selection');
  app.run("job={id:'handoff-a',version:1}");const current=app.run('generateHandoff()');complete();await current;
  assert.equal(app.element('handoff-preview').hidden,false);
  assert.equal(app.run('handoff.jobId'),'handoff-a');
  assert.ok(paths.every(([,method])=>method==='GET'));
  app.run('logout()');assert.equal(app.run('handoff'),null);assert.equal(app.element('handoff-content').textContent,'');
  app.run("token='synthetic';job={id:'handoff-a',version:1}");binding.modelRevision={raw:'PRIVATE_PROVIDER'};
  const malformed=app.run('generateHandoff()');complete();await assert.rejects(malformed,/格式不完整/);assert.equal(app.run('handoff'),null);
}
console.log('PASS: handoff blocks unsaved changes, ignores late task replies, stays read-only and clears on logout');

{
  const requests=[];
  const app=workspace(async path=>{requests.push(new URL('http://local'+path).searchParams);return response({items:[],hasMore:false,statusCounts:{failed:1},actionRequiredCount:1});});
  app.run("token='synthetic';jobsOffset=60");app.element('job-classroom').value='c & status=failed';
  app.element('job-classroom').listeners.change({target:app.element('job-classroom')});await flush();
  assert.equal(requests[0].get('classroom'),'c & status=failed','class IDs remain one encoded parameter');
  assert.equal(requests[0].get('offset'),'0');assert.equal(requests[0].get('status'),'');
  app.element('jobs-reset').listeners.click({target:app.element('jobs-reset')});await flush();
  assert.equal(requests[1].get('classroom'),'');assert.equal(app.element('job-classroom').value,'');
  app.element('job-classroom').value='previous-account-class';app.run('logout()');
  assert.equal(app.element('job-classroom').value,'','account-specific selection must be removed on logout');
}
{
  let resolveOld,oldSignal;
  const app=workspace((path,options)=>{
    if(!resolveOld)return new Promise(resolve=>{resolveOld=resolve;oldSignal=options.signal;});
    return Promise.resolve(response({items:[],hasMore:false,statusCounts:{failed:2},actionRequiredCount:2}));
  });
  app.run("token='synthetic'");app.element('job-classroom').value='old';const old=app.run('refreshJobs(30)');
  app.element('job-classroom').value='new';await app.run('refreshJobs(0)');
  assert.equal(oldSignal.aborted,true);
  resolveOld(response({items:[],hasMore:true,statusCounts:{failed:999},actionRequiredCount:999}));await old;
  assert.equal(app.run('jobsOffset'),0);assert.equal(app.element('job-classroom').value,'new');
  assert.equal(app.element('jobs-next').disabled,true,'an old class response cannot replace new pagination');
}
console.log('PASS: classroom filtering composes, resets, clears account state and rejects stale replies');

{
  const replies=[];
  const app=workspace(()=>new Promise(resolve=>replies.push(resolve)));
  app.run("token='synthetic';me={role:'teacher'};");
  const older=app.run('loadLearning()'),newer=app.run('loadLearning()');
  const result=title=>({proposals:[{rubricId:title,cases:1,state:'submitted',sourceValid:true,reasons:[],hasProposedRubric:false}],experiments:[]});
  replies[1](response(result('Latest proposal')));await newer;
  replies[0](response(result('Stale proposal')));await older;
  assert.ok(app.element('learning-list').children[0].children[0].textContent.includes('Latest proposal'),'late learning response must not replace the latest proposal list');
}
console.log('PASS: learning list keeps the latest request when responses arrive out of order');

{
  const calls=[];
  const app=workspace(async(path,options)=>{calls.push({path,options});return {ok:false,status:400,json:async()=>({error:{code:'INTENT_UNCLEAR'}})};});
  app.run("token='synthetic';rubrics=[{id:'test',languagePolicy:'en',genre:'short-text'}];");
  app.element('rubric').value='test';app.element('intent').value='auto';
  app.element('intent').listeners.change?.();
  assert.equal(app.element('operator-request').required,true,'automatic intent must mark teacher requirements as required');
  app.element('operator-request').value='   ';
  app.element('create-form').listeners.submit({preventDefault(){},submitter:app.element('submit')});await flush();
  assert.equal(calls.length,0,'blank teacher requirements must not create a job');
  assert.equal(app.element('operator-request').focused,true);
  assert.ok(app.element('operator-request').validationMessage.includes('处理要求'));
  app.element('intent').value='assess';app.element('intent').listeners.change?.();
  assert.equal(app.element('operator-request').required,false);assert.equal(app.element('operator-request').validationMessage,'');
}
console.log('PASS: automatic intent validates teacher requirements before submission');

{
  let finishOld, oldSignal;
  const app=workspace((path,options)=>{
    if(!finishOld)return new Promise(resolve=>{finishOld=resolve;oldSignal=options.signal;});
    return Promise.resolve(response({items:[{title:'New session reference',body:'Synthetic current content',sources:[]}]}));
  });
  app.run("token='synthetic-same-token';me={role:'teacher'};");
  const submit=()=>app.element('library-form').listeners.submit({preventDefault(){},submitter:app.element('library-submit')});
  submit();await flush();app.run("logout();token='synthetic-same-token';me={role:'teacher'};");
  submit();await flush();finishOld(response({items:[{title:'Old session reference',body:'Synthetic stale content',sources:[]}]}));await flush();
  assert.equal(app.element('library-list').children[0].children[0].textContent,'New session reference','old same-token session must not replace the new reference list');
  assert.equal(oldSignal.aborted,true,'logout must cancel the old reference read');
}
{
  let finish;
  const app=workspace(()=>new Promise(resolve=>finish=resolve));
  app.run("token='synthetic-same-token';me={role:'teacher'}");const old=app.run("api('/library')");
  app.run("logout();token='synthetic-same-token';me={role:'teacher',tenantId:'new-session'}");
  finish({ok:false,status:401,json:async()=>({error:{code:'UNAUTHENTICATED'}})});
  await assert.rejects(old,error=>error.name==='AbortError');
  assert.equal(app.run('me.tenantId'),'new-session','late unauthorized response must not clear a new same-token session');
}
console.log('PASS: reference reads and authentication failures cannot cross same-token sessions');

for(const oldFailure of [false,true]){
  let finishOld,oldSignal,logins=0;
  const app=workspace((path,options)=>{
    if(path.endsWith('/me'))return Promise.resolve(response({tenantId:++logins===1?'old-login':'new-login',role:'teacher',classroomIds:[]}));
    if(path.endsWith('/rubrics'))return Promise.resolve(response({items:[]}));
    if(path==='/health/ready'&&logins===1)return new Promise(resolve=>{finishOld=resolve;oldSignal=options.signal;});
    if(path==='/health/ready')return Promise.resolve(response({modelConfigured:false,ocrConfigured:false}));
    return Promise.resolve(response({items:[],hasMore:false}));
  });
  const login=()=>app.element('login-form').listeners.submit({preventDefault(){},submitter:app.element('login-submit')});
  app.element('token').value='synthetic-same-token';login();await flush();assert.ok(finishOld);
  app.run('logout()');app.element('token').value='synthetic-same-token';login();await flush();
  finishOld(oldFailure?{ok:false,status:503,json:async()=>({})}:response({modelConfigured:true,ocrConfigured:true}));await flush();
  assert.equal(app.run('me?.tenantId'),'new-login','late health reply must not clear the new login');
  assert.equal(app.element('workspace').hidden,false);assert.ok(app.element('service-status').textContent.includes('待配置'));
  assert.equal(oldSignal?.aborted,true,'logout cancels the old login initialization');
}
console.log('PASS: late successful and failed health reads cannot overwrite a later login');

// A successful create must finish client acknowledgement, not ask the teacher to repeat it.
{
  const posts=[];
  const app=workspace(async(path,options)=>{posts.push({path,options});return response({id:'synthetic-created'});});
  app.run("token='synthetic';rubrics=[{id:'test',languagePolicy:'en',genre:'short-text'}];dirty=true;refreshJobs=async()=>{};selectJob=async id=>{job={id};return true;};");
  app.element('rubric').value='test';app.element('new-text').value='Synthetic writing';
  app.element('create-form').listeners.submit({preventDefault(){},submitter:app.element('submit')});await flush();
  assert.equal(app.run('job?.id'),'synthetic-created','successful create must open the server-accepted task');
  assert.equal(app.run('dirty'),false);assert.equal(app.run('createKey'),null);assert.equal(posts.length,1);
  assert.ok(posts[0].options.headers['Idempotency-Key']);assert.ok(!app.element('message').textContent.includes('session is not defined'));
}
console.log('PASS: successful creation acknowledges one accepted task and clears retry state');

{
  let calls=0;const app=workspace(async()=>{calls++;return response({});});
  app.run("token='synthetic';job={id:'pending',version:1,status:'needs_text_review'};dirty=false;");
  app.element('export-job').listeners.click();await flush();
  assert.equal(calls,0,'unready printable exports must stop before a request or empty download');
  assert.match(app.element('message').textContent,/检查|就绪/);
}
console.log('PASS: printable export requires a current successful readiness check');

{
 const app=workspace(async()=>response(readyFixture()));app.run("token='synthetic';job={id:'a',version:2};");
 await app.run('checkReportReadiness()');assert.equal(app.element('export-job').disabled,false);assert.equal(app.element('readiness-checks').children.length,9);
 app.run('job.version++;renderReadiness()');assert.equal(app.element('export-job').disabled,true);assert.equal(app.element('readiness-checks').children.length,0);
}
{
 let finish;const app=workspace(()=>new Promise(resolve=>finish=resolve));app.run("token='synthetic';job={id:'a',version:2};");
 const pending=app.run('checkReportReadiness()');app.run("selectionVersion++;job={id:'b',version:1};clearReadiness()");finish(response(readyFixture()));await pending;
 assert.equal(app.run('reportReadiness'),null);assert.equal(app.element('export-job').disabled,true);
}
{
 const bad={...readyFixture(),checks:[readyFixture().checks[0]]};const app=workspace(async()=>response(bad));app.run("token='synthetic';job={id:'a',version:2};");
 await assert.rejects(app.run('checkReportReadiness()'),/不完整/);assert.equal(app.element('export-job').disabled,true);
}
console.log('PASS: readiness is version-bound, rejects incomplete responses and ignores late task replies');

for(const reason of ['REPORT_TOTAL_MISMATCH','TEACHER_TOTAL_MISMATCH']){
 const data=readyFixture();data.canExport=false;data.state='blocked';data.nextAction='inspect_events';
 data.checks=data.checks.map(item=>item.code==='report_content'?{...item,state:'blocked',reason}:item);
 let calls=0;const app=workspace(async()=>{calls++;return response(data);});app.run("token='synthetic';job={id:'a',version:2};");
 await app.run('checkReportReadiness()');
 assert.equal(app.element('export-job').disabled,true);
 assert.match(app.element('readiness-checks').children.find(item=>item.textContent.includes('总分')).textContent,/维度评分|权重/);
 app.element('export-job').listeners.click();await flush();assert.equal(calls,1);
}
console.log('PASS: inconsistent totals explain the problem and keep printing disabled');


{
 const calls=[];const app=workspace(async(path,options)=>{calls.push([path,options.method]);return response({items:[],limit:3});});
 app.run("token='synthetic';me={role:'teacher'};job={id:'source',version:4,status:'approved',review:{scores:{grammar:3}},result:{scores:[]},payload:{}};revisionListLoaded=true;dirty=true;revisionDirty=true;");
 app.element('review-comment').value='Unpublished teacher edit';app.element('revision-text').value='Unsaved manual revision';
 app.run("setJobStage('revisions');setJobStage('report');setJobStage('source');setJobStage('revisions')");
 assert.equal(app.element('review-comment').value,'Unpublished teacher edit');assert.equal(app.element('revision-text').value,'Unsaved manual revision');assert.equal(app.run('dirty'),true);assert.equal(app.run('revisionDirty'),true);assert.equal(calls.length,0,'view switches must not submit or reload form state');
 app.run("setJobStage('report');focusJobTarget('text-form')");assert.equal(app.run('jobStage'),'source');assert.equal(app.element('stage-source-panel').hidden,false);assert.equal(app.element('text-form').focused,true);
 app.run("job.status='needs_text_review';renderRevisionAvailability()");assert.equal(app.element('stage-revisions').disabled,true);assert.equal(app.run("setJobStage('revisions')"),false);
 console.log('PASS: task views retain both edit states, route focus to the visible original and preserve revision eligibility');
}
{
 const calls=[];const app=workspace(async(path,options)=>{calls.push([path,options.method]);if(path.endsWith('/reassessment'))return response({schema:'inkwise.revision-assessment/v1',revisionId:'r',revisionVersion:2,status:'not_requested',job:null,after:null,delta:null});return response({id:'r',ordinal:1,version:2,status:'confirmed',source:{textVersion:1},text:'Revised',note:'',resolutions:{},feedback:[],diff:{parts:[]}});});
 app.run("token='synthetic';me={role:'teacher'};job={id:'source',version:4,status:'approved',payload:{},review:{scores:{grammar:3}},result:{scores:[]}};");await app.run("openWritingRevision('r')");
 assert.equal(app.run('revisionAssessment.status'),'not_requested');assert.equal(calls.length,2);assert.ok(calls.every(([,method])=>method==='GET'));assert.equal(app.element('reassessment-submit').disabled,true,'read-only opening never grants submission consent');assert.equal(app.element('reassessment-consent').hidden,false);
 console.log('PASS: opening a confirmed revision reads its saved reassessment once, with no automatic submission or polling');
}

{
 let finish;const app=workspace(()=>new Promise(resolve=>finish=resolve));
 app.run("token='synthetic';me={role:'teacher'};job={id:'source',payload:{}};writingRevision={id:'old',version:2,status:'confirmed'};");
 const pending=app.run('fetchReassessment()');app.run("clearReassessment();writingRevision={id:'new',version:1,status:'draft'};");finish(response({schema:'inkwise.revision-assessment/v1',revisionId:'old',revisionVersion:2,status:'reviewed',after:null,delta:null}));await pending;
 assert.equal(app.run('revisionAssessment'),null);assert.equal(app.element('reassessment-comparison').children.length,0);assert.equal(app.element('reassessment-submit').disabled,true);
 console.log('PASS: a late saved assessment cannot cross revision selection or enable another request');
}

{
 const app=workspace(async()=>{throw Error('synthetic selection outage');});
 app.run("token='synthetic';me={role:'teacher'};job={id:'source',status:'approved',payload:{title:'Synthetic'}};writingRevision={id:'draft',version:2,status:'draft'};revisionDirty=true;");app.element('revision-text').value='Unsaved synthetic revision';
 await app.run("selectJob('unavailable')").catch(()=>{});
 assert.equal(app.run('job.id'),'source');assert.equal(app.element('revision-text').value,'Unsaved synthetic revision','failed selection must not discard a manual revision');assert.equal(app.run('revisionDirty'),true);
 console.log('PASS: failed job reads preserve the current manual revision and unsaved protection');
}

function returnFixture(status='failed'){
 const scores=Object.fromEntries(['content','coherence','expression','vocabulary','grammar'].map(name=>[name,3]));
 const origin={sourceJobId:'source',revisionId:'revision',revisionVersion:3};
 return {origin,child:{id:'child',version:2,status,classroomId:'c',payload:{title:'Synthetic child',grade:7,revisionAssessment:origin},text:'Revised',textVersion:1},parent:{id:'source',version:4,status:'approved',classroomId:'c',payload:{title:'Synthetic original',grade:7},text:'Original',textVersion:1,review:{scores},result:{scores:[]}},revision:{id:'revision',ordinal:1,version:3,status:'confirmed',source:{textVersion:1},text:'Revised',note:'',resolutions:{},feedback:[],diff:{parts:[]}},assessment:{schema:'inkwise.revision-assessment/v1',revisionId:'revision',revisionVersion:3,status,job:{id:'child',status},after:null,delta:null}};
}
for(const status of ['failed','cancelled']){
 const fixture=returnFixture(status),calls=[];
 const app=workspace(async(path,options)=>{calls.push([path,options.method]);if(path==='/api/v1/jobs/source')return response(fixture.parent);if(path.endsWith('/events'))return response({items:[]});if(path.endsWith('/reassessment'))return response(fixture.assessment);if(path.endsWith('/revision'))return response(fixture.revision);if(path.endsWith('/revisions'))return response({items:[{id:'revision',ordinal:1,status:'confirmed'}],limit:3});throw Error('Unexpected synthetic read');});
 app.context.childFixture=fixture.child;app.run("token='synthetic';me={role:'teacher'};job=childFixture;");app.element('workspace').hidden=false;app.element('job-panel').hidden=false;app.run('renderReassessmentSource()');assert.equal(app.element('reassessment-source').hidden,false);
 assert.equal(await app.run('returnToSourceRevision()'),true);assert.equal(app.run('job.id'),'source');assert.equal(app.run('writingRevision.id'),'revision');assert.equal(app.run('revisionAssessment.status'),status);assert.equal(app.run('jobStage'),'revisions');assert.equal(app.element('reassessment-heading').focused,true);assert.ok(calls.every(([,method])=>method==='GET'));
}
console.log('PASS: failed/cancelled child returns to the exact original revision and reads saved state without resubmission');
{
 const fixture=returnFixture(),calls=[];const app=workspace(async(path,options)=>{calls.push(path);return {ok:false,status:404,json:async()=>({error:{code:'NOT_FOUND'}})};});app.context.childFixture=fixture.child;app.run("token='synthetic';me={role:'teacher'};job=childFixture;dirty=true;");app.element('workspace').hidden=false;app.element('job-panel').hidden=false;app.element('review-comment').value='Unsaved child review';app.context.window.confirm=()=>false;
 assert.equal(await app.run('returnToSourceRevision()'),false);assert.equal(calls.length,0);assert.equal(app.element('review-comment').value,'Unsaved child review');
 app.context.window.confirm=()=>true;await app.run('returnToSourceRevision()').catch(()=>{});assert.equal(app.run('job.id'),'child');assert.equal(app.run('dirty'),true);assert.equal(app.element('job-panel').inert,false);assert.equal(app.element('review-comment').value,'Unsaved child review');
 app.run("dirty=false;job.status='running'");await app.run("selectJob('forbidden')").catch(()=>{});assert.ok([...app.timers.values()].some(timer=>timer.delay===2000));
}
console.log('PASS: return respects edit confirmation and denied source access retains the current view and running-status polling');
{
 const calls=[];const app=workspace(async(path)=>{calls.push(path);return response({});});app.run("token='synthetic';job={id:'child',status:'failed',payload:{revisionAssessment:{sourceJobId:'../foreign',revisionId:'revision',revisionVersion:3}}};");app.element('workspace').hidden=false;app.element('job-panel').hidden=false;app.run('renderReassessmentSource()');assert.equal(app.element('reassessment-source').hidden,true);assert.equal(await app.run('returnToSourceRevision()'),false);assert.equal(calls.length,0);
 app.run("job.payload.revisionAssessment={sourceJobId:'child',revisionId:'revision',revisionVersion:3}");assert.equal(app.run('reassessmentSource()'),null);
}
{
 let old;let oldSignal;const fixture=returnFixture();const app=workspace(async(path,options)=>{if(path==='/api/v1/jobs/source'){oldSignal=options.signal;return new Promise(resolve=>old=resolve);}if(path.endsWith('/events'))return response({items:[]});return response({...fixture.parent,id:'other',status:'needs_text_review'});});app.context.childFixture=fixture.child;app.run("token='synthetic';job=childFixture;me={role:'teacher'};");app.element('workspace').hidden=false;app.element('job-panel').hidden=false;
 const returning=app.run('returnToSourceRevision()');assert.equal(app.element('job-panel').inert,true);await app.run("selectJob('other')");assert.equal(oldSignal.aborted,true);old(response(fixture.parent));assert.equal(await returning,false);assert.equal(app.run('job.id'),'other');assert.equal(app.element('job-panel').inert,false);
}
console.log('PASS: invalid origin and late return cannot open another scope or override a newer job selection');

{
 const app=workspace((path,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(Error('aborted synthetic read'),{name:'AbortError'})),{once:true})));
 app.run("token='synthetic';job={id:'source',status:'approved',payload:{title:'Synthetic'}};writingRevision={id:'draft',version:1,status:'draft'};revisionDirty=true;");app.element('revision-text').value='Retained draft during timeout';const pending=app.run("selectJob('slow')");const [id,deadline]=[...app.timers].find(([,timer])=>timer.delay===30000);app.timers.delete(id);deadline.fn();await assert.rejects(pending,/读取超时/);assert.equal(app.element('job-panel').inert,false);assert.equal(app.element('revision-text').value,'Retained draft during timeout');assert.equal(app.run('revisionDirty'),true);
 console.log('PASS: stalled job reads time out without leaving the view locked or losing current edits');
}

{
 const app=workspace(async()=>response({id:'case-a',jobId:'a',reportVersion:1,status:'pending',version:1}));
 app.run("token='synthetic';me={userId:'teacher',role:'teacher'};job={id:'a',version:1};dirty=true");
 for(const id of ['candidate-text','candidate-comment','provenance'])app.element(id).value='Synthetic submitted content';app.element('reuse-confirmed').checked=true;
 app.element('candidate-form').listeners.submit({preventDefault(){},submitter:app.element('submit')});await flush();await flush();
 assert(app.element('candidate-status').textContent.includes('case-a'));
 assert.equal(app.element('candidate-text').value,'');assert.equal(app.element('candidate-comment').value,'');assert.equal(app.element('open-candidate').disabled,false);
}
console.log('PASS: candidate save exposes its exact receipt and a review reopening action');
{
 let finish;const app=workspace(async()=>new Promise(resolve=>finish=()=>resolve(response({id:'case-a',jobId:'a',reportVersion:1,status:'pending',version:1}))));
 app.run("token='synthetic';job={id:'a',version:1};dirty=true");app.element('candidate-text').value='Submitted synthetic text';
 app.element('candidate-form').listeners.submit({preventDefault(){},submitter:app.element('submit')});await flush();
 app.element('workspace').listeners.input({target:{closest:()=>({id:'experiment-form',dataset:{}})}});finish();await flush();await flush();
 assert.equal(app.run('dirty'),true,'a completed candidate cannot clear newer unsaved work');
}
console.log('PASS: candidate completion retains newer same-job edits');

{
 const requests=[],detail={id:'case-target',jobId:'a',reportVersion:1,status:'rejected',version:2,text:'Synthetic case',provenance:'Synthetic permission',review:{comment:'Synthetic comment',feedback:{}},submittedBy:'teacher',decisionReason:'Synthetic review reason'};
 const app=workspace(async path=>{requests.push(path);return response(detail);});app.run("token='synthetic';me={userId:'teacher',role:'teacher'};job={id:'a',version:1};$('workspace').hidden=false;$('knowledge-panel').hidden=true");
 await app.run("openKnowledge('case-target','a')");assert.deepEqual(requests,['/api/v1/candidates/case-target']);assert.equal(app.element('knowledge-panel').hidden,false);assert.equal(app.element('knowledge-list').children[0].dataset.candidateId,'case-target');assert.equal(app.element('candidates-next').disabled,true);
}
console.log('PASS: a saved candidate opens by exact identifier without depending on the first history page');
{
 let fail=true;const app=workspace(async(path,options)=>options.method==='POST'?{ok:false,status:503,json:async()=>({error:{code:'STORAGE_UNAVAILABLE'}})}:response({jobId:'a',reportVersion:1,candidate:null}));
 app.run("token='synthetic';job={id:'a',version:1};dirty=true");app.element('candidate-text').value='Preserved synthetic draft';app.element('reuse-confirmed').checked=true;
 app.element('candidate-form').listeners.submit({preventDefault(){},submitter:app.element('submit')});await flush();await flush();assert.equal(app.element('candidate-text').value,'Preserved synthetic draft');assert.equal(app.run('dirty'),true);assert.equal(app.element('submit-candidate').disabled,true);assert(app.element('candidate-status').textContent.includes('尚未确认'));
 await app.run('readCandidateStatus()');assert.equal(app.element('submit-candidate').disabled,false);assert.equal(app.element('candidate-text').value,'Preserved synthetic draft');
}
console.log('PASS: uncertain submissions keep their draft and require a readback before another attempt');
{
 const app=workspace(async()=>response({jobId:'other',reportVersion:1,candidate:null}));app.run("token='synthetic';job={id:'a',version:1};candidateKey=candidateIdentity();");await app.run('readCandidateStatus()');assert.equal(app.element('submit-candidate').disabled,true);assert(app.element('candidate-status').textContent.includes('版本不一致'));
}
console.log('PASS: mismatched candidate readbacks cannot enable a submission for another job');

{
 const item={id:'case-a',jobId:'a',reportVersion:1,status:'pending',version:1,text:'Saved synthetic A',provenance:'Synthetic permission',review:{comment:'A',feedback:{}},submittedBy:'teacher'};
 const app=workspace(async()=>response({...item,version:2}));app.context.item=item;app.run("token='synthetic';me={userId:'teacher',role:'teacher'};job={id:'a',version:1};$('workspace').hidden=false;$('knowledge-panel').hidden=false;");
 const cardA={dataset:{candidateId:'case-a'},replaceWith(value){this.updated=value;}},cardB={dataset:{candidateId:'case-b'},replaceWith(){throw Error('Another draft was replaced');}};
 app.element('knowledge-list').querySelectorAll=()=>[cardA,cardB];app.element('case-b-draft').value='Preserve synthetic B';
 for(const id of ['case-a','case-b'])app.element('workspace').listeners.input({target:{closest:()=>({id:'case-edit',dataset:{unsaved:'true',candidateId:id}})}});
 await app.run("savedCaseAction(item,'/candidates/case-a/revisions',{version:1})");assert(cardA.updated);assert.equal(app.element('case-b-draft').value,'Preserve synthetic B');assert.equal(app.run("candidateEdits.has('case-b')"),true);assert.equal(app.run('dirty'),true);
}
console.log('PASS: saving one candidate keeps another candidate draft and its unsaved protection');


{
 const calls=[],app=workspace(async(url)=>{calls.push(url);return response({items:[{id:'current',classroomId:'class-a',status:'teacher_review'},{id:'other-class',classroomId:'class-b',status:'teacher_review'},{id:'next',classroomId:'class-a',status:'needs_text_review'}],hasMore:false});});
 app.run("token='synthetic';me={role:'teacher'};job={id:'current',version:2,classroomId:'class-a',status:'approved'};");app.element('job-search').value='synthetic title';app.context.selected=[];
 app.run("selectJob=async id=>{selected.push(id);job={...job,id};return true;}");assert.equal(await app.run('openNextPendingTask()'),true);assert.equal(app.context.selected[0],'next');assert.equal(app.element('job-attention').value,'needs_action');
 const query=new URL('http://local'+calls[0]);assert.equal(query.searchParams.get('classroom'),'class-a');assert.equal(query.searchParams.get('search'),'synthetic title');assert.equal(query.searchParams.get('attention'),'needs_action');assert.equal(query.searchParams.get('view'),'summary');assert.equal(calls.length,1);
 app.run("dirty=true;window.confirm=()=>false;");await app.run('openNextPendingTask()');assert.equal(calls.length,1,'declined unsaved edits never trigger navigation or a request');
}
{
 let finish;const app=workspace(()=>new Promise(resolve=>finish=resolve));app.run("token='synthetic';job={id:'current',version:2,classroomId:'class-a'};selectJob=async()=>{throw Error('stale result must not navigate')};");
 const pending=app.run('openNextPendingTask()');app.run('editVersion++;dirty=true;');finish(response({items:[{id:'next',classroomId:'class-a',status:'teacher_review'}],hasMore:false}));await pending;
 assert.equal(app.run('job.id'),'current');assert.ok(app.element('next-task-status').textContent.includes('新修改'));assert.equal(app.run('dirty'),true);
}
console.log('PASS: next task uses one scoped metadata query, protects unsaved edits and rejects late navigation');

{
 let finish;const app=workspace(()=>new Promise(resolve=>finish=resolve));app.run("token='synthetic';job={id:'current',version:2,classroomId:'class-a'};selectJob=async()=>{throw Error('changed classroom must not navigate')};");
 const pending=app.run('openNextPendingTask()');app.element('job-classroom').value='class-b';finish(response({items:[{id:'next',classroomId:'class-a',status:'teacher_review'}],hasMore:false}));await pending;
 assert.equal(app.run('job.id'),'current');assert.ok(app.element('next-task-status').textContent.includes('筛选已变化'));
 app.run('renderQueueLanes({approved:2,needs_text_review:1},1)');assert.equal(app.element('lane-approved').textContent,'教师已确认（2）');app.run('logout()');assert.equal(app.element('lane-approved').textContent,'教师已确认（0）');
}
console.log('PASS: changed classroom rejects an old next-task response and account exit clears lane counts');

{
 const app=workspace(async()=>response({}));app.run("job={id:'saved',version:2};dirty=true;clearReadiness();");app.element('readiness-status').textContent='有未保存的修改，请先保存或确认后再检查报告。';app.run('dirty=false;renderReadiness();');
 assert.ok(!app.element('readiness-status').textContent.includes('未保存的修改'),'a saved result must not retain the old unsaved warning');assert.ok(app.element('readiness-status').textContent.includes('尚未检查'));assert.equal(app.element('export-job').disabled,true,'saving is not readiness acceptance');
}
console.log('PASS: saved report clears stale unsaved warning without granting unchecked export');


{
 const calls=[],app=workspace(async(path,options)=>{calls.push({path,options});return response({id:'accepted-task'});});
 app.run("token='synthetic';rubrics=[{id:'test',languagePolicy:'en',genre:'short-text'}];dirty=true;refreshJobs=async()=>{throw Error('List unavailable after accepted creation')};selectJob=async id=>{job={id};return true;};");app.element('rubric').value='test';app.element('new-text').value='Synthetic writing';
 app.element('create-form').listeners.submit({preventDefault(){},submitter:app.element('submit')});await flush();
 assert.equal(app.run('job?.id'),'accepted-task','an accepted create must open directly even when list refresh fails');assert.equal(calls.length,1);
}
console.log('PASS: accepted creation opens directly despite a failed metadata list refresh');


{
 let late;const calls=[],app=workspace((path,options)=>{calls.push({path,options});return calls.length===1?new Promise(resolve=>late=resolve):Promise.resolve(response({id:'same-created-task'}));});
 app.run("token='synthetic';rubrics=[{id:'test',languagePolicy:'en',genre:'short-text'}];dirty=true;refreshJobs=async()=>{};selectJob=async id=>{if(!discardEdits())return false;job={id};dirty=false;return true;};");app.element('rubric').value='test';app.element('new-text').value='Original synthetic essay';
 app.element('create-form').listeners.submit({preventDefault(){},submitter:app.element('create-submit')});await flush();const deadline=[...app.timers.values()].find(timer=>timer.delay===30000);assert(deadline,'creation has a bounded wait');deadline.fn();await flush();
 assert.equal(app.run('createRecovery.state'),'unknown');const key=app.run('createKey');assert(key);assert.equal(app.element('create-recovery').hidden,false);
 late(response({id:'late-reply'}));await flush();assert.equal(app.run('job'),null,'late success never silently navigates after timeout');
 app.element('new-text').value='New unsaved synthetic edits';app.run('editVersion++;dirty=true;window.confirm=()=>false;');app.element('create-form').listeners.submit({preventDefault(){},submitter:app.element('create-submit')});await flush();assert.equal(calls.length,1,'unknown outcome cannot allocate another key');
 await app.element('create-recover').listeners.click({target:app.element('create-recover')});await flush();assert.equal(calls.length,2);assert.equal(calls[0].options.headers['Idempotency-Key'],calls[1].options.headers['Idempotency-Key']);assert.equal(JSON.parse(calls[1].options.body).text,'Original synthetic essay');assert.equal(app.run('dirty'),true);assert.equal(app.run('job'),null,'declined new edits protect the current page');
 app.run('window.confirm=()=>true;');await app.element('create-recover').listeners.click({target:app.element('create-recover')});await flush();assert.equal(calls.length,2,'known receipt opens by ID without another POST');assert.equal(app.run('job.id'),'same-created-task');assert.equal(app.run('createRecovery'),null);
}
{
 let late;const app=workspace(()=>new Promise(resolve=>late=resolve));app.run("token='synthetic';rubrics=[{id:'test',languagePolicy:'en',genre:'short-text'}];dirty=true;");app.element('rubric').value='test';app.element('new-text').value='Private synthetic text';app.element('create-form').listeners.submit({preventDefault(){},submitter:app.element('create-submit')});await flush();
 app.run('logout();');late(response({id:'old-session-task'}));await flush();assert.equal(app.run('createRecovery'),null);assert.equal(app.run('createKey'),null);assert.equal(app.run('job'),null);assert.equal(app.element('create-receipt-id').textContent,'');assert.equal(app.element('new-text').value,'');
}
console.log('PASS: bounded unknown creation, exact same-key/payload retry, preserved new edits, known-ID opening and logout isolation');


{
 let finish;const calls=[],app=workspace((path,options)=>{calls.push({path,options});if(options.method==='POST')return new Promise(resolve=>finish=resolve);return Promise.resolve(response({id:'a',version:2,status:'queued',payload:{title:'Synthetic'}}));});app.run("token='synthetic';job={id:'a',version:1,status:'needs_text_review'};dirty=true;");app.element('confirmed-text').value='Unsaved synthetic edit';
 const pending=app.run("mutateCurrentJob('text-confirmations',{method:'POST',body:{version:1,text:'Synthetic'}})");await flush();const timer=[...app.timers.values()].find(item=>item.delay===30000);assert(timer);timer.fn();await flush();assert.equal(await pending,false);assert.equal(app.run('mutationRecovery.state'),'unknown');finish(response({id:'a',version:2,payload:{title:'Synthetic'}}));await flush();assert.equal(app.run('job.version'),1);
 await app.element('mutation-read').listeners.click({target:app.element('mutation-read')});await flush();assert.equal(app.run('mutationRecovery.observed.version'),2);assert.equal(app.run('job.version'),1);assert.equal(app.element('confirmed-text').value,'Unsaved synthetic edit');assert.equal(calls.filter(call=>call.options.method==='POST').length,1);
 await assert.rejects(app.run("mutateCurrentJob('text-confirmations',{method:'POST',body:{version:1}})"),/先读取/);app.run('logout()');assert.equal(app.run('mutationRecovery'),null);assert.equal(app.element('mutation-state').textContent,'');
}
{
 let finish;const app=workspace(()=>new Promise(resolve=>finish=resolve));app.run("token='synthetic';job={id:'a',version:1};");const pending=app.run("mutateCurrentJob('reviews',{method:'POST',body:{version:1}})");app.run('editVersion++;dirty=true;');finish(response({id:'a',version:2,status:'approved',payload:{}}));assert.equal(await pending,false);assert.equal(app.run('job.version'),1);assert.equal(app.run('dirty'),true);assert.equal(app.run('mutationRecovery.observed.version'),2);
}
{
 const app=workspace(async()=>response({id:'a',version:1,status:'needs_text_review'}));app.run("token='synthetic';job={id:'a',version:1};mutationRecovery={id:'a',version:1,session:sessionGeneration,selection:selectionVersion,intent:selectionRequest,state:'unknown',observed:null};selectionRequest++;");await app.element('mutation-read').listeners.click({target:app.element('mutation-read')});await flush();assert.equal(app.run('mutationRecovery.observed.version'),1);app.element('mutation-keep').listeners.click();assert.equal(app.run('mutationRecovery'),null);
}
{
 let calls=0;const app=workspace(async()=>{calls++;return response({})});app.run("job={id:'image-task',version:2,status:'failed',hasImage:true,textVersion:0,errorCode:'OCR_FAILED'};renderUploadIntent();");assert.equal(app.element('upload-mode').value,'choose');app.element('image-file').files=[{size:100,name:'synthetic.png'}];app.element('upload-form').listeners.submit({preventDefault(){},submitter:app.element('upload-submit')});await flush();assert.equal(calls,0);assert.ok(app.element('message').textContent.includes('明确选择'));
}
console.log('PASS: mutation deadlines, no automatic resend, read-only reconciliation, failed-selection recovery and explicit page intent');

{
 let finish;const app=workspace(()=>new Promise(resolve=>finish=resolve));app.run("token='synthetic';job={id:'stop-task',version:1};");const pending=app.run("mutateCurrentJob('cancel',{method:'POST',body:{version:1}})");app.element('mutation-stop').listeners.click();await flush();assert.equal(await pending,false);assert.equal(app.run('mutationRecovery.state'),'unknown');finish(response({id:'stop-task',version:2,payload:{}}));await flush();assert.equal(app.run('job.version'),1);
}
console.log('PASS: explicitly stopping client wait never commits a late mutation response');

{
 const app=workspace(async()=>response({}));app.run("token='synthetic';job={id:'queued-task',version:1,status:'queued'};mutationRecovery={id:job.id,version:1,session:sessionGeneration};schedulePoll();");
 assert.equal(app.run('timer'),null,'an unresolved mutation must pause background reads even after a failed navigation');
}
console.log('PASS: unresolved mutations retain version ownership while background polling is paused');

{
 const requests=[];let fail=false;
 const app=workspace(async(path)=>{requests.push(new URL('http://local'+path).searchParams);if(fail)throw Error('synthetic list unavailable');return response({items:[{id:'sort-job',classroomId:'class-a',status:'teacher_review',payload:{title:'Synthetic sorted job'}}],hasMore:false,statusCounts:{teacher_review:1}});});
 app.run("token='synthetic'");app.element('job-sort').value='created';await app.run('refreshJobs(30)');
 app.element('job-sort').value='updated';app.element('job-sort').listeners.change({target:app.element('job-sort')});await flush();
 assert.equal(requests[1].get('sort'),'updated');assert.equal(requests[1].get('offset'),'0');assert.equal(app.element('job-list').children.length,1);
 fail=true;app.element('job-sort').value='created';app.element('job-sort').listeners.change({target:app.element('job-sort')});await flush();
 assert.equal(app.element('job-sort').value,'updated','failed refresh must restore the order of the retained list');assert.equal(app.element('job-list').children.length,1);
 app.run('logout()');assert.equal(app.element('job-sort').value,'created');
 console.log('PASS: queue sorting resets pagination, preserves failed lists and clears account state');
}

{
 const requests=[];let fail=false;
 const app=workspace(async(path)=>{requests.push(new URL('http://local'+path).searchParams);if(fail)throw Error('synthetic candidate outage');return response({items:[],hasMore:false});});
 app.run("token='synthetic'");app.element('candidate-classroom').value='class-a';app.element('candidate-filter').value='pending';app.element('candidate-search').value='  Garden & 100%  ';
 await app.run('changeCandidateFilters()');
 assert.equal(requests[0].get('offset'),'0');assert.equal(requests[0].get('classroom'),'class-a');assert.equal(requests[0].get('status'),'pending');assert.equal(requests[0].get('search'),'Garden & 100%');
 fail=true;app.element('candidate-filter').value='rejected';await assert.rejects(app.run('changeCandidateFilters()'));assert.equal(app.element('candidate-filter').value,'pending');
 app.run('logout()');assert.equal(app.element('candidate-classroom').value,'');assert.equal(app.element('candidate-filter').value,'');assert.equal(app.element('candidate-search').value,'');
 console.log('PASS: candidate lookup composes, resets paging, restores failed criteria and clears on logout');
}

{
 const requests=[];let fail=false;
 const item={id:'case-filter',jobId:'source',reportVersion:1,status:'pending',version:1,text:'SYNTHETIC_RETAINED_CASE',submittedBy:'other'};
 const app=workspace(async(path)=>{requests.push(path);if(fail)throw Error('synthetic failed candidate search');return response({items:[item],hasMore:false});});
 app.run("token='synthetic';me={role:'teacher',userId:'teacher'}");app.element('candidate-filter').value='pending';await app.run('changeCandidateFilters()');
 const retained=app.element('knowledge-list').children[0];app.run("dirty=true;candidateEdits.set('case-filter',1)");app.context.window.confirm=()=>false;
 app.element('candidate-filter').value='rejected';assert.equal(await app.run('changeCandidateFilters()'),false);assert.equal(requests.length,1);assert.equal(app.element('candidate-filter').value,'pending');assert.equal(app.element('knowledge-list').children[0],retained);assert.equal(app.run('candidateEdits.size'),1);
 app.context.window.confirm=()=>true;fail=true;app.element('candidate-search').value='missing';await assert.rejects(app.run('changeCandidateFilters()'));assert.equal(app.element('knowledge-list').children[0],retained);assert.equal(app.run('candidateEdits.size'),1);assert.equal(app.element('candidate-search').value,'');
 console.log('PASS: cancelled or failed candidate filtering preserves mounted cases and unsaved drafts');
}

{
 const requests=[];let fail=false;
 const item={id:'case-first',jobId:'job-first',version:1,reportVersion:1,status:'pending',submittedBy:'teacher',classroomId:'class-a',text:'Synthetic anonymized case',provenance:'Synthetic permission',review:{comment:'Synthetic'},sourceCurrent:true,sourceVersion:1,sourceStatus:'approved'};
 const app=workspace(async path=>{requests.push(new URL('http://local'+path).searchParams);if(fail)throw Error('synthetic review outage');return response({item,nextAfter:item.id,order:'oldest-created'});});
 app.run("token='synthetic';me={role:'reviewer',userId:'reviewer',classroomIds:['class-a']};");app.element('knowledge-panel').hidden=false;app.element('candidate-classroom').value='class-a';
 await app.run('nextCandidateReview()');assert.equal(app.run('candidateReviewCursor'),'case-first');assert.equal(requests[0].get('after_id'),null);
 const card=app.element('knowledge-list').children[0];app.run("candidateEdits.set('case-first',1);dirty=true");app.context.window.confirm=()=>false;
 assert.equal(await app.run('nextCandidateReview()'),false);assert.equal(requests.length,1);assert.equal(app.element('knowledge-list').children[0],card);
 app.run('candidateEdits.clear();dirty=false');fail=true;await assert.rejects(app.run('nextCandidateReview()'));assert.equal(app.element('knowledge-list').children[0],card);assert.equal(app.run('candidateReviewCursor'),'case-first');
 app.run('logout()');assert.equal(app.run('candidateReviewCursor'),'');assert.equal(app.run('candidateReviewScope'),'');
 console.log('PASS: continuous candidate review preserves unsaved/failing cards and resets its scoped cursor on account exit');
}
{
 let release;const app=workspace(()=>new Promise(resolve=>{release=resolve}));
 app.run("token='synthetic';me={role:'reviewer',userId:'reviewer',classroomIds:['class-a']};");app.element('knowledge-panel').hidden=false;
 const pending=app.run('nextCandidateReview()');await flush();app.run('logout()');
 release(response({item:{id:'old-case',jobId:'old-job',status:'pending'},nextAfter:'old-case',order:'oldest-created'}));await pending;
 assert.equal(app.element('knowledge-list').children.length,0);assert.equal(app.run('candidateReviewCursor'),'');
 console.log('PASS: a delayed review-queue response cannot restore a previous account candidate');
}


{
 const app=workspace(async()=>response({}));app.run("clearNotice();notify('原稿已保留');");
 assert.equal(app.element('message').hidden,false,'new feedback must be visible after the previous notice was cleared');
 app.run('logout()');assert.equal(app.element('message').hidden,true);
 console.log('PASS: cleared notices become visible again and account exit hides them');
}
for(const code of ['STORAGE_CAPACITY_EXCEEDED','IMAGE_STORAGE_CAPACITY_EXCEEDED']){
 const app=workspace(async()=>({ok:false,status:507,json:async()=>({error:{code}})}));
 app.run("token='synthetic';job={id:'budget',version:1};dirty=true;");
 await assert.rejects(app.run("mutateCurrentJob('image',{method:'POST',body:{version:1}})"),/存储/);
 assert.equal(app.run('mutationRecovery'),null);assert.equal(app.run('job.version'),1);assert.equal(app.run('dirty'),true);
}
{
 const app=workspace(async()=>({ok:false,status:503,json:async()=>({error:{code:'IMAGE_STORAGE_WRITE_FAILED'}})}));
 app.run("token='synthetic';job={id:'uncertain-image',version:1};dirty=true;");
 assert.equal(await app.run("mutateCurrentJob('image',{method:'POST',body:{version:1}})"),false);
 assert.equal(app.run('mutationRecovery.state'),'unknown');assert.equal(app.run('dirty'),true);
 console.log('PASS: explicit image capacity refusals show a next step; uncertain writes retain reconciliation');
}

{
  const reads = [];
  const app = workspace((_url, options) => new Promise((_resolve, reject) => {
    reads.push(options.signal);
    options.signal?.addEventListener('abort', () => reject(new Error('aborted')), {once:true});
  }));
  app.run("job={id:'synthetic-original'}; token='fixture'");
  const first = app.run('loadImage()');
  const second = app.run('loadImage()');
  assert.equal(reads[0]?.aborted, true, 'switching pages must cancel the old image stream');
  await first;
  const deadline = [...app.timers.values()].find(timer => timer.delay === 30000);
  assert.ok(deadline, 'a stalled image stream must have a deadline');
  deadline.fn();
  await assert.rejects(second, /原稿读取超时/);
  assert.equal(app.element('original-image').hidden, true);
  assert.equal(reads[1].aborted, true);
  console.log('PASS: superseded and timed-out original image reads release their streams');
}
