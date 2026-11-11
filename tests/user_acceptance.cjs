/* User journeys across teacher/reviewer/admin. Reuses an existing synthetic local-model report. */
const assert=require('node:assert/strict');
const base = new URL(process.env.INKWISE_PREVIEW_URL || 'http://127.0.0.1:8766');
assert(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname) && base.protocol === 'http:' && !base.username && !base.password, 'Use the loopback synthetic preview');
const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1365,height:900}});page.on('dialog',dialog=>dialog.accept());
  async function login(token){await page.goto(new URL('/workspace/', base).href);await page.locator('#token').fill(token);await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});}
  async function cases(){await page.locator('#show-knowledge').click();await page.locator('#knowledge-panel').waitFor({state:'visible'});}
  const card=()=>page.locator('#knowledge-list article').filter({hasText:'Independent synthetic review case'}).first();
  await login('synthetic-reviewer-preview-only');await cases();
  await card().getByLabel('审核依据',{exact:true}).fill('请补充案例的使用授权说明。');
  await card().getByRole('button',{name:'退回候选',exact:true}).click();
  await page.waitForFunction(()=>[...document.querySelectorAll('#knowledge-list article')].find(node=>node.textContent.includes('Independent synthetic review case'))?.textContent.includes('已退回'));
  await login('synthetic-local-preview-only');await cases();
  assert((await card().textContent()).includes('请补充案例的使用授权说明。'),'The submitting teacher cannot see why the case was returned');
  await card().getByRole('button',{name:'修改候选',exact:true}).click();
  await card().getByLabel('来源与授权说明',{exact:true}).fill('Original synthetic fixture, authorization checked again.');
  await card().getByLabel('已核对本次修改的脱敏内容并取得使用授权',{exact:true}).check();
  await card().getByRole('button',{name:'保存修改并重新提交审核',exact:true}).click();
  await page.waitForFunction(()=>[...document.querySelectorAll('#knowledge-list article')].find(node=>node.textContent.includes('Independent synthetic review case'))?.textContent.includes('待审核'));
  await login('synthetic-reviewer-preview-only');await cases();
  await card().getByLabel('审核依据',{exact:true}).fill('已核对补充的合成案例说明。');await card().getByRole('button',{name:'批准候选',exact:true}).click();
  await page.waitForFunction(()=>[...document.querySelectorAll('#knowledge-list article')].find(node=>node.textContent.includes('Independent synthetic review case'))?.textContent.includes('已审核'));
  await login('synthetic-admin-preview-only');await cases();await page.locator('#load-learning').click();await page.locator('#run-learning').waitFor({state:'visible'});await page.locator('#run-learning').click();
  await page.waitForFunction(()=>document.getElementById('learning-list').textContent.includes('尚未满足'));assert(!(await page.locator('#learning-list').textContent()).includes('PINNED_MODEL'));assert((await page.locator('#learning-list').textContent()).includes('已审核案例数量不足'));
  await login('synthetic-local-preview-only');
  await page.locator('#job-search').fill('已有本机模型报告');await page.locator('#job-search-form button').click();
  await page.locator('#job-list button').filter({hasText:'已有本机模型报告'}).click();await page.locator('#review-form').waitFor({state:'visible'});
  assert.equal((await page.locator('#confirmed-text').inputValue()).length,15000);
  assert.equal(await page.locator('#review-scores input[type=number]').count(),5);
  await page.locator('#explain-report').click();await page.waitForFunction(()=>document.querySelectorAll('#explanation h4').length===5);
  await page.locator('#review-comment').fill('本次仅核对已保存报告的使用流程，不作为教学效果签核。');await page.locator('#feedback-confirmation').check();await page.locator('#review-form button').click();
  await page.waitForFunction(()=>document.getElementById('job-status').textContent==='教师已确认');
  const [readiness] = await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/report-readiness')),
    page.locator('#check-report-readiness').click(),
  ]);
  const checked = await readiness.json();
  assert.equal(checked.canExport, true, 'Preserved report binding must permit printable export: ' + JSON.stringify(checked.checks?.map(item=>[item.code,item.status])));
  await page.locator('#export-job:not([disabled])').waitFor();
  const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#export-job').click()]);
  const report=fs.readFileSync(await download.path(),'utf8');assert(report.includes('本次仅核对已保存报告的使用流程'));assert(!report.includes('尚未由教师确认'));
  await page.locator('#job-search').fill('');await page.locator('#job-search-form button').click();await page.locator('#new-job').click();await page.locator('#sample-text').click();await page.locator('#title').fill('取消流程验收');await page.locator('#create-form button.primary').click();
  await page.locator('#cancel-job').click();await page.waitForFunction(()=>document.getElementById('job-status').textContent==='已取消');
  assert(await page.locator('#cancel-job').isDisabled());
  await page.locator('#new-job').click();await page.locator('#sample-text').click();await page.locator('#title').fill('失败恢复验收');await page.locator('#create-form button.primary').click();await page.locator('#confirm-checkbox').check();await page.locator('#text-form button').click();
  await page.waitForFunction(()=>document.getElementById('job-status').textContent==='处理未完成');
  assert((await page.locator('#job-error').textContent()).includes('尚未配置评分模型'));assert((await page.locator('#confirmed-text').inputValue()).length>0);
  await page.locator('#retry-job').click();await page.waitForFunction(()=>document.getElementById('job-status').textContent==='处理未完成');
  console.log('PASS: user acceptance across teacher/reviewer/admin, rejection reason and resubmission, existing real-model long report review/explanation/export, cancellation and safe failed-task recovery.');
 }finally{await browser.close();}
})().catch(error=>{console.error(error.message);process.exit(1)});
