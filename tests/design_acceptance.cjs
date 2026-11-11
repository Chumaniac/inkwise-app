/* Local synthetic UI checks; no model requests and no deployment. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const axeSource=fs.readFileSync(process.env.AXE_SOURCE||require.resolve('axe-core/axe.min.js'),'utf8');
const output='artifacts/redesign';fs.mkdirSync(output,{recursive:true});
const results=[],consoleErrors=[];
const viewports=[320,390,768,1024,1440];
async function inspect(page,name){
 await page.evaluate(()=>document.fonts.ready);
 await page.evaluate(axeSource);
 const audit=await page.evaluate(async()=>{
  const result=await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}});
  return {violations:result.violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)})),incomplete:result.incomplete.map(v=>({id:v.id,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))};
 });
 results.push({name,...audit});
 if(audit.violations.length)throw Error(name+' accessibility: '+JSON.stringify(audit.violations));
 for(const width of viewports){
  await page.setViewportSize({width,height:1000});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,name+' overflows at '+width);
  const background=await page.evaluate(()=>getComputedStyle(document.body).backgroundColor);
  assert.equal(background,'rgb(248, 249, 250)',name+' must retain the light background');
 }
 await page.setViewportSize({width:1440,height:1000});
 await page.evaluate(()=>document.documentElement.style.fontSize='200%');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,name+' overflows with 200% text');
 await page.evaluate(()=>document.documentElement.style.fontSize='');
 await page.emulateMedia({colorScheme:'dark',reducedMotion:'reduce'});
 assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).colorScheme),'light',name+' switched into dark mode');
 const durations=await page.locator('button').evaluateAll(items=>items.map(item=>getComputedStyle(item).transitionDuration));
 assert(durations.every(value=>value.split(',').every(v=>parseFloat(v)===0)),name+' must honor reduced motion');
 await page.emulateMedia({colorScheme:'light',reducedMotion:'no-preference'});
 await page.evaluate(()=>{document.activeElement?.blur();window.scrollTo(0,0);});
 await page.screenshot({path:`${output}/after-${name}.png`});
 await page.setViewportSize({width:390,height:844});
 await page.evaluate(()=>window.scrollTo(0,0));

 await page.screenshot({path:`${output}/after-${name}-mobile.png`});
 await page.setViewportSize({width:1440,height:1000});
}
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',error=>consoleErrors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')consoleErrors.push(message.text().slice(0,160)+' '+message.location().url);});
  await page.goto('http://127.0.0.1:8766/workspace/');
  await page.keyboard.press('Tab');assert.equal(await page.locator('.skip').evaluate(e=>e===document.activeElement),true);
  await page.keyboard.press('Enter');assert.equal(await page.locator('#main').evaluate(e=>e===document.activeElement),true);
  await inspect(page,'login');
  await page.locator('#token').fill('synthetic-local-preview-only');await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});
  await inspect(page,'create');
  await page.locator('#job-list button').filter({hasText:'合成教师编辑测试'}).click();await page.locator('#review-form').waitFor({state:'visible'});
  await inspect(page,'report');
  await page.locator('#zoom-original').click();await page.locator('#image-dialog').waitFor({state:'visible'});
  assert.equal(await page.locator('#image-dialog').getAttribute('aria-labelledby'),'image-dialog-title');
  for(let i=0;i<4;i++){await page.keyboard.press('Tab');assert.equal(await page.evaluate(()=>!!document.activeElement.closest('#image-dialog')),true,'Dialog focus escaped');}
  await page.screenshot({path:`${output}/after-dialog.png`});
  await page.keyboard.press('Escape');await page.locator('#image-dialog').waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.activeElement.id==='zoom-original');
  await page.locator('#show-knowledge').click();await page.locator('#knowledge-panel').waitFor({state:'visible'});await inspect(page,'knowledge');
  await page.goto('http://127.0.0.1:8767/');await page.locator('#report .scorecircle').waitFor();await inspect(page,'demo');
  for(const [view,name] of [['knowledge','demo-knowledge'],['architecture','demo-architecture']]){
   await page.locator(`[data-view="${view}"]`).click();await page.locator(`#${view}-view`).waitFor({state:'visible'});await inspect(page,name);
  }
  for(const name of ['technical-design','engineering-status']){await page.goto(`http://127.0.0.1:8767/${name}.html`);await inspect(page,name);}
  assert.deepEqual(consoleErrors,[],'Unexpected browser or CSP error');
  fs.writeFileSync(`${output}/design-check.json`,JSON.stringify({views:results,viewports,textZoom:'200% simulated root text size',osDarkPreference:'stays light',reducedMotion:'verified',keyboard:'skip link, native controls, modal trap/Escape/return focus',consoleErrors},null,2));
  console.log(`PASS: ${results.length} views; 320/390/768/1024/1440 px; 200% text; dark preference remains light; reduced motion; keyboard modal; axe WCAG A/AA checks.`);
 }finally{await browser.close();}
})().catch(error=>{fs.writeFileSync(`${output}/design-check-failure.json`,JSON.stringify({message:error.message,results,consoleErrors},null,2));console.error(error.message);process.exit(1)});
