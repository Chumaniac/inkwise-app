// Synthetic local cases only; candidate lookup never approves, publishes or grades.
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:960}}),errors=[];let writes=0;
  page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{if(request.method()==='POST')writes++});
  await page.goto(process.env.INKWISE_PREVIEW_URL+'/workspace/');await page.locator('#token').fill('synthetic-local-preview-only');await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});await page.locator('#show-knowledge').click();
  const cards=page.locator('#knowledge-list .candidate');await expect(cards).toHaveCount(20);
  await page.locator('#candidate-filter').selectOption('pending');await expect(cards).toHaveCount(1);await expect(cards).toContainText('独立审核往返案例');
  await page.locator('#candidate-search').fill('no-synthetic-match');await page.locator('#candidate-search-form button').click();await expect(cards).toHaveCount(0);await expect(page.locator('#candidates-page')).toContainText('符合当前条件');
  await page.locator('#candidate-reset').click();await expect(cards).toHaveCount(20);
  await page.locator('#candidate-classroom').selectOption('demo-class');await page.locator('#candidate-filter').selectOption('rejected');await page.locator('#candidate-search').fill('历史练习');await page.locator('#candidate-search-form button').click();await expect(cards).toHaveCount(20);
  await page.locator('#candidates-next').click();await expect(cards).toHaveCount(2);
  const ids=await cards.evaluateAll(nodes=>nodes.map(node=>node.dataset.candidateId));
  await page.route('**/api/v1/candidates?**',route=>route.abort('failed'),{times:1});await page.locator('#candidate-filter').selectOption('pending');
  await expect(page.locator('#candidate-filter')).toHaveValue('rejected');await expect(page.locator('#candidates-page')).toContainText('尚未更新');expect(await cards.evaluateAll(nodes=>nodes.map(node=>node.dataset.candidateId))).toEqual(ids);
  await page.locator('#candidate-reset').click();await page.locator('#candidate-filter').selectOption('pending');await expect(cards).toHaveCount(1);
  await cards.getByRole('button',{name:'修改候选',exact:true}).click();const draft=cards.locator('textarea').first();await draft.fill('SYNTHETIC_UNSAVED_CASE_EDIT');page.once('dialog',dialog=>dialog.dismiss());await page.locator('#candidate-filter').selectOption('rejected');await expect(page.locator('#candidate-filter')).toHaveValue('pending');await expect(draft).toHaveValue('SYNTHETIC_UNSAVED_CASE_EDIT');
  expect(writes).toBe(0);await page.setViewportSize({width:390,height:844});await page.locator('#candidate-search').scrollIntoViewIfNeeded();expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  if(process.env.CANDIDATE_SCREENSHOT)await page.screenshot({path:process.env.CANDIDATE_SCREENSHOT});
  page.once('dialog',dialog=>dialog.accept());await page.locator('#logout').click();await expect(page.locator('#candidate-search')).toHaveValue('');await expect(page.locator('#candidate-filter')).toHaveValue('');await expect(cards).toHaveCount(0);expect(errors).toEqual([]);
  console.log('PASS: scoped candidate state/class/title lookup, pagination/empty/reset, failed-list rollback, cancelled unsaved edits, no writes, mobile/logout');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
