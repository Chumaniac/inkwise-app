// Run only against the independent synthetic report-readiness fixture.
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{const browser=await chromium.launch({channel:'chrome',headless:true});try{
 const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[],exports=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().includes('/export-snapshot'))exports.push(r.url());});
 await page.goto(process.env.READINESS_URL);await page.locator('#token').fill('synthetic-citation-only');await page.locator('#login-form button').click();await expect(page.locator('#workspace')).toBeVisible();
 await page.locator('#job-list button').filter({hasText:'未就绪合成任务'}).click();await expect(page.locator('#export-job')).toBeDisabled();await page.locator('#check-report-readiness').click();await expect(page.locator('#readiness-status')).toContainText('尚未就绪');await expect(page.locator('#readiness-checks')).toContainText('当前任务尚未形成');expect(exports).toHaveLength(0);
 await page.locator('#readiness-next').click();expect(page.url()).toContain('#text-form');
 await page.locator('#job-list button').filter({hasText:'报告就绪合成示例'}).click();await page.locator('#check-report-readiness').click();await expect(page.locator('#export-job')).toBeEnabled();await expect(page.locator('#export-job')).toHaveText('下载待教师复核稿');
 const pending=page.waitForEvent('download');await page.locator('#export-job').click();const received=await pending,html=require('node:fs').readFileSync(await received.path(),'utf8');expect(received.suggestedFilename()).toBe('inkwise-report-draft.html');expect(html).toContain('尚未由教师确认');expect(html).toContain('原文 SHA-256');
 expect(exports).toHaveLength(1);expect(exports[0]).toContain('purpose=printable');expect(errors).toEqual([]);
 await page.locator('#report-readiness').scrollIntoViewIfNeeded();if(process.env.READINESS_SCREENSHOT)await page.screenshot({path:process.env.READINESS_SCREENSHOT});
 await page.setViewportSize({width:390,height:844});await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
 await page.locator('#logout').click();await expect(page.locator('#readiness-checks')).toHaveText('');await expect(page.locator('#export-job')).toBeDisabled();
 console.log('PASS: unready report explains next action and blocks empty export; bound report downloads as an explicit teacher-review draft; scope/mobile/logout');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
