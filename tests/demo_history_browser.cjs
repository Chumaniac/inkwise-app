const assert=require('node:assert/strict');
const http=require('node:http');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const root=path.resolve(__dirname,'../demo/dist');
 const server=http.createServer(async(req,res)=>{try{const file=path.resolve(root,'.'+(req.url==='/'?'/index.html':req.url));if(!file.startsWith(root+path.sep))throw Error();res.setHeader('content-type',({'.js':'text/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream');res.end(await fs.readFile(file));}catch{res.statusCode=404;res.end();}});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage();page.on('dialog',d=>d.accept());
 try{
  await page.goto('http://127.0.0.1:'+server.address().port);await page.locator('.teacher summary').click();
  await page.locator('#score-0').fill('999');await page.locator('#save-demo-report').click();
  assert.equal(await page.locator('.history-row').count(),0,'invalid edited scores must not silently save the earlier grades');
  assert.match(await page.locator('#demo-history-state').innerText(),/分数|评分/);
  await page.locator('#score-0').fill('12');await page.locator('#teacher-comment').fill('合成案例：请补充具体观察。');await page.locator('#save-demo-report').click();
  assert.equal(await page.locator('.history-row').count(),1);await page.reload();await page.locator('.history-row').getByRole('button',{name:'打开报告',exact:true}).click();
  assert.equal(await page.locator('#score-0').inputValue(),'12');assert.equal(await page.locator('#teacher-comment').inputValue(),'合成案例：请补充具体观察。');
  console.log('PASS: invalid grade saves refused and edited grades/comments survive refresh and reopening');
 }finally{await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;});
