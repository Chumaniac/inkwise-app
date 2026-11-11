const base=process.env.INKWISE_PREVIEW_URL||'http://127.0.0.1:14983';
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
const fs=require('node:fs/promises');
(async()=>{const browser=await chromium.launch({channel:'chrome'});try{
 const context=await browser.newContext({viewport:{width:390,height:844}}),page=await context.newPage();page.on('dialog',dialog=>dialog.accept());const writes=[];page.on('request',request=>{if(request.url().includes('/api/v1/')&&!['GET','HEAD'].includes(request.method()))writes.push(request.method())});
 await page.goto(base+'/workspace/');await page.locator('#token').fill('synthetic-local-preview-only');await page.locator('#login-form button').click();await expect(page.locator('#workspace')).toBeVisible();
 const chosen=page.locator('#job-list button').filter({hasText:'合成教师编辑测试'});await chosen.click();await expect(page.locator('#job-panel')).toBeVisible();
 await page.locator('#confirmed-text').fill('UNSAVED_PRIVATE_TEXT');await page.locator('#generate-handoff').click();await expect(page.locator('#message')).toContainText('先保存');await expect(page.locator('#handoff-preview')).toBeHidden();
 await chosen.click();await page.locator('#generate-handoff').click();await expect(page.locator('#handoff-preview')).toBeVisible();
 const current=JSON.parse(await page.locator('#handoff-content').textContent());expect(current.jobId).toBe('synthetic-feedback-edit');expect(current.nextAction).toBe('review_report');
 const downloadPromise=page.waitForEvent('download');await page.locator('#download-handoff').click();const download=await downloadPromise;const bytes=await fs.readFile(await download.path(),'utf8');expect(JSON.parse(bytes)).toEqual(current);for(const field of ['text','payload','result','review','ocr','token'])expect(Object.hasOwn(current,field)).toBe(false);expect(bytes).not.toMatch(/MODEL_DRAFT_SENTINEL|Synthetic teacher review test|synthetic-local-preview-only/);
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 if(process.env.AXE_MODULE){const AxeBuilder=require(process.env.AXE_MODULE).default;expect((await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze()).violations).toEqual([])}
 if(process.env.INKWISE_HANDOFF_SCREENSHOT)await page.screenshot({path:process.env.INKWISE_HANDOFF_SCREENSHOT});
 await page.locator('#new-job').click();await expect(page.locator('#handoff-preview')).toBeHidden();await page.locator('#logout').click();await expect(page.locator('#handoff-content')).toHaveText('');expect(writes).toEqual([]);console.log('PASS real synthetic API → version-bound handoff → JSON download; no student/report/token data; 0 business writes; unsaved edits / selection / logout / mobile');
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exitCode=1});
