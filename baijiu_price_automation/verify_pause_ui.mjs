import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
const base=new URL('./',import.meta.url);
const browser=await chromium.launch({headless:true,channel:'msedge'});
const errors=[],checks=[];
let status={running:false,message:'等待操作',log:'',result:null,progress:0,job_id:null};
const config={workbook_options:[{value:'fixture.xlsx',label:'隔离测试数据库'}],workbooks:['fixture.xlsx'],default_workbook:'fixture.xlsx',database_versions:[],start:'2026-10-01',end:'2026-10-02',inputs:0};
try {
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    const routes={'/api/config':config,'/api/overview':{accounts:['易茅时价','今日酒价','茅粉鲁智深','酒价参考','国酒财经','国酒大数据'],products:14,latest_run:null},'/api/status':status};
    if(url.pathname==='/api/run')status={...status,running:true,pausable:true,job_id:'fixture-job',mode:'rpa',progress:37,message:'正在采集公众号 3/6：酒价参考'};
    if(url.pathname==='/api/pause'){
      assert.equal(route.request().postDataJSON().job_id,status.job_id);
      status={...status,pause_requested:true,paused:false};
    }
    if(url.pathname==='/api/resume')status={...status,pause_requested:false,paused:false};
    if(url.pathname in routes)return route.fulfill({json:routes[url.pathname]});
    if(['/api/run','/api/pause','/api/resume'].includes(url.pathname))return route.fulfill({json:{ok:true}});
    const name=url.pathname==='/'?'ui.html':url.pathname.slice(1);
    if(['ui.html','ui.js','ui.css'].includes(name))return route.fulfill({body:await fs.readFile(new URL(name,base)),contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
    return route.fulfill({status:404,body:''});
  });
  await page.goto('http://127.0.0.1:18765/',{waitUntil:'networkidle'});
  assert.equal(await page.locator('#pause-job').isVisible(),false);
  await page.locator('#run').click();
  await page.locator('#pause-job').waitFor();
  await page.locator('#pause-job').click();
  await page.locator('#progress-title').filter({hasText:'正在暂停'}).waitFor();
  assert.equal(await page.locator('#resume-job').textContent(),'取消暂停');
  status={...status,paused:true};
  await page.locator('#progress-title').filter({hasText:'任务已暂停'}).waitFor();
  assert.equal(await page.locator('#progress-bar').getAttribute('value'),'37');
  assert.equal(await page.locator('#run').isDisabled(),true);
  assert.equal(await page.locator('#start').isDisabled(),true);
  assert.equal(await page.locator('#completion').getAttribute('open'),null);
  await page.reload({waitUntil:'networkidle'});
  await page.locator('#progress-title').filter({hasText:'任务已暂停'}).waitFor();
  assert.equal(await page.locator('#progress-bar').getAttribute('value'),'37');
  await page.screenshot({path:new URL('./outputs/pause-ui.png',base).pathname.slice(1),fullPage:true});
  await page.setViewportSize({width:390,height:900});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.locator('#resume-job').click();
  await page.locator('#pause-job').waitFor();
  assert.equal(await page.locator('#progress-bar').getAttribute('value'),'37');
  status={...status,pausable:false,progress:90};
  await page.locator('#pause-job').waitFor({state:'hidden'});
  status={...status,running:false,progress:100,message:'更新完成',result:{mode:'rpa',prices:2,cells:4,excel:'fixture.xlsx',files:[],preview:[],gaps:[]}};
  await page.locator('#completion[open]').waitFor();
  assert.equal(await page.locator('#resume-job').isVisible(),false);
  assert.deepEqual(errors,[]);
  checks.push('pause request and confirmation','progress retained after page reload','resume same task','configuration stays locked','pause hidden while writing','completion dialog only on finish','mobile width without overflow');
  await fs.writeFile(new URL('./outputs/pause_ui_verification.json',base),JSON.stringify({checks,errors,method:'Isolated browser requests with actual workbench HTML/JS/CSS; no live WeChat operations'},null,2));
  console.log(JSON.stringify({checks,errors}));
}finally{await browser.close();}
