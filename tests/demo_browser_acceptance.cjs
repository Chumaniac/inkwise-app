const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{const browser=await chromium.launch({channel:'chrome',headless:true});try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 await page.goto(process.env.INKWISE_DEMO_URL||'http://127.0.0.1:8767/');
 await page.locator('#sample').selectOption('en');
 await page.locator('#essay').fill('This is an original synthetic browser fixture. It must never receive an invented model grade.');
 await page.locator('#text-confirmed').check();await page.keyboard.press('Control+Enter');
 await page.waitForFunction(()=>document.querySelector('#report .scorecircle strong').textContent==='—');
 await page.locator('.teacher summary').click();
 for(const [i,value] of [20,18,15,10,6].entries())await page.locator(`#score-${i}`).fill(String(value));
 await page.locator('#teacher-comment').fill('Synthetic teacher decision, not model output.');
 await page.locator('#allow-reuse').check();await page.locator('#approve').click();
 assert((await page.locator('#toast').textContent()).includes('自定义文字不进入'));
 const pending=page.waitForEvent('download');await page.getByRole('button',{name:'导出报告',exact:true}).click();
 const download=await pending,report=JSON.parse(fs.readFileSync(await download.path(),'utf8'));
 assert.equal(report.mode,'manual-review');assert.equal(report.approved,true);assert.deepEqual(report.scores,[20,18,15,10,6]);
 await page.locator('[data-view="knowledge"]').click();assert.equal((await page.locator('#candidate-count').textContent()).trim(),'0 条');
 await page.locator('[data-view="work"]').click();await page.locator('#reset').click();
 await page.locator('#allow-reuse').check();await page.locator('#approve').click();
 await page.locator('[data-view="knowledge"]').click();assert.equal((await page.locator('#candidate-count').textContent()).trim(),'1 条');
 await page.getByRole('button',{name:'撤回候选',exact:true}).click();assert.equal((await page.locator('#candidate-count').textContent()).trim(),'0 条');
 await page.locator('[data-view="architecture"]').click();await page.locator('#architecture-view').waitFor({state:'visible'});
 await page.setViewportSize({width:390,height:844});
 for(const view of ['work','knowledge','architecture']){
  await page.locator(`[data-view="${view}"]`).click();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),`Mobile overflow in ${view}`);
 }
 console.log('PASS: redesigned demo navigation, manual-only custom grading, keyboard shortcut, teacher review/export, synthetic-only reuse and withdrawal.');
}finally{await browser.close();}})().catch(e=>{console.error(e.message);process.exit(1)});
