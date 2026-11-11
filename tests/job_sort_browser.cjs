// Only synthetic queue reads; no OCR, grading, publication or student records.
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:960}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto((process.env.INKWISE_PREVIEW_URL||'http://127.0.0.1:8766')+'/workspace/');
  await page.locator('#token').fill('synthetic-local-preview-only');await page.locator('#login-form button').click();await page.locator('#workspace').waitFor({state:'visible'});
  await page.locator('#job-attention').selectOption('');await expect(page.locator('#job-list button')).toHaveCount(30);
  const read=page.waitForResponse(response=>response.url().includes('/api/v1/jobs?')&&response.url().includes('sort=updated'));
  await page.locator('#job-sort').selectOption('updated');const data=await (await read).json();
  await expect.poll(()=>page.locator('#job-list button').evaluateAll(nodes=>nodes.map(node=>node.dataset.jobId))).toEqual(data.items.map(row=>row.id));
  expect(data.sort).toBe('updated');expect(data.items.some(row=>['text','rubric','result','review'].some(key=>Object.hasOwn(row,key)))).toBe(false);
  await expect(page.locator('#job-list button').first()).toContainText('更新');
  await page.locator('#jobs-next').click();await expect(page.locator('#jobs-page')).toContainText('第 31');
  await page.locator('#job-sort').selectOption('created');await expect(page.locator('#jobs-page')).toContainText('第 1–30');
  const before=await page.locator('#job-list button').evaluateAll(nodes=>nodes.map(node=>node.dataset.jobId));
  await page.route('**/api/v1/jobs?**',route=>route.abort('failed'),{times:1});await page.locator('#job-sort').selectOption('updated');
  await expect(page.locator('#job-sort')).toHaveValue('created');await expect(page.locator('#jobs-page')).toContainText('尚未更新');
  expect(await page.locator('#job-list button').evaluateAll(nodes=>nodes.map(node=>node.dataset.jobId))).toEqual(before);
  await page.locator('#job-sort').selectOption('updated');await expect(page.locator('#jobs-page')).toContainText('第 1–30');
  await page.setViewportSize({width:390,height:844});await page.locator('#job-sort').scrollIntoViewIfNeeded();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  if(process.env.JOB_SORT_SCREENSHOT)await page.screenshot({path:process.env.JOB_SORT_SCREENSHOT});
  await page.locator('#logout').click();await expect(page.locator('#job-sort')).toHaveValue('created');expect(errors).toEqual([]);
  console.log('PASS: real scoped API ordering, summary privacy, pagination reset, failed-refresh rollback, mobile and logout');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
