const {chromium, request} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
 const port = process.env.CQ_SPINNER_PORT || '8087';
 assert.ok(['8087','8088'].includes(port));
 const baseURL = 'http://127.0.0.1:'+port;
 const username = process.env.CQ_SPINNER_USER;
 const password = process.env.CQ_SPINNER_PASSWORD;
 assert.ok(username?.startsWith('cq-spinner-') && password, 'Use a dedicated temporary cq-spinner-* account');
 const api = '/ocs/v2.php/apps/codex_queue/api/v1';
 const client = await request.newContext({baseURL, httpCredentials: {username,password}, extraHTTPHeaders:{Authorization:'Basic '+Buffer.from(username+':'+password).toString('base64'),'OCS-APIRequest':'true','Accept':'application/json'}});
 const call = async (method, path, data) => {
  if (data?.results?.length > 20) {
   for (let i=0; i<data.results.length; i+=20) await call(method,path,{...data,results:data.results.slice(i,i+20)});
   return;
  }
  const response = await client[method](api + path + '?format=json', data ? {data} : {});
  assert.ok(response.ok(), `${method} ${path}: ${response.status()}`);
  return (await response.json()).ocs.data;
 };
 const old = await call('get','/board');
 assert.equal(old.pending.length, 0, 'The isolated test board must have no pending requests');
 const snapshot = {projects:{999999:'Spinner verification'}, ideas:[{id:999999, project_id:999999,title:'Spinner verification',detail:''}],tasks:[]};
 const browser = await chromium.launch({channel:'msedge',headless:true});
 let own = [], codex, codexResult;
 let codexFinished = false;
 const isolated = fsTemp();
 function fsTemp() {return require('fs').mkdtempSync(require('path').join(require('os').tmpdir(),'cq-spinner-'));}
 try {
  await call('post','/bridge',{snapshot,results:[]});
  const page = await browser.newPage();
  await page.goto(baseURL+'/login');
  await page.locator('input[name="user"]').fill(username);
  await page.locator('input[name="password"]').fill(password);
  await page.locator('button[type="submit"], input[type="submit"]').first().click();
  await page.waitForURL(url => !url.pathname.includes('login'));
  await page.goto(baseURL+'/index.php/apps/codex_queue/');
  const plan = page.locator('[data-request-kind="plan"][data-request-target="999999"]');
  await plan.waitFor();
  await plan.click();
  await page.waitForFunction(() => document.querySelector('[data-key="idea-999999"]').getAttribute('aria-busy') === 'true');
  const incoming = await call('get','/bridge');
  own = incoming.requests;
  assert.equal(own.length,1);
  if (process.env.CQ_SPINNER_CODEX_ENTRY) {
   codex = require('child_process').spawn(process.execPath, [process.env.CQ_SPINNER_CODEX_ENTRY,'exec','--ignore-user-config','--ephemeral','--sandbox','read-only','--skip-git-repo-check','-C',isolated,
    'This is an isolated UI waiting indicator smoke test. Execute the PowerShell command Start-Sleep -Seconds 12, then return exactly SPINNER_OK. Do not inspect files, network services or credentials.'], {stdio:['ignore','pipe','pipe'],windowsHide:true});
   let output = '';
   codex.stdout.on('data',data=>output+=data);
   codex.stderr.on('data',data=>output+=data);
   codexResult = new Promise(resolve=>{
    const timeout = setTimeout(()=>codex.kill(),120000);
    codex.on('error',()=>{clearTimeout(timeout);codexFinished=true;resolve({code:-1,output});});
    codex.on('exit',code=>{clearTimeout(timeout);codexFinished=true;resolve({code,output});});
   });
  }
  for (let i=0;i<25;i++) await call('post','/requests',{kind:'sync',target:0});
  own = (await call('get','/bridge')).requests;
  await call('post','/bridge',{snapshot,results:own.filter(r=>r.kind==='sync').map(r=>({id:r.id,outcome:'Spinner history verification'}))});
  const rest = (await call('get','/bridge')).requests;
  own = [...new Map([...own,...rest].map(r=>[r.id,r])).values()];
  await call('post','/bridge',{snapshot,results:own.filter(r=>r.kind==='sync').map(r=>({id:r.id,outcome:'Spinner history verification'}))});
  const longHistory = await call('get','/board');
  assert.equal(longHistory.requests.length,20);
  assert.equal(longHistory.pending.length,1);
  // Hold the bridge completion through multiple real five-second browser polls.
  await new Promise(r => setTimeout(r, 6200));
  assert.equal(await plan.getAttribute('aria-busy'),'true');
  if (codex) assert.equal(codexFinished,false,'Real Codex must still be running during the UI check');
  assert.equal(await page.locator('#cq-open-add').isEnabled(),true);
  await page.locator('#cq-open-add').click();
  assert.equal(await page.locator('#cq-add-dialog').evaluate(el => el.open),true);
  await page.locator('#cq-cancel-add').click();
  await page.reload();
  await plan.waitFor();
  assert.equal(await plan.getAttribute('aria-busy'),'true');
  if (codexResult) {
   const result = await codexResult;
   assert.equal(result.code,0,'Codex smoke execution failed');
   assert.ok(result.output.includes('SPINNER_OK'));
   console.log('Actual Codex execution while bridge request was pending: PASS');
  }
  await call('post','/bridge',{snapshot,results:own.map(r => ({id:r.id,outcome:'Spinner verification completed'}))});
  await page.waitForFunction(() => document.querySelector('[data-key="idea-999999"]').getAttribute('aria-busy') === 'false');
  console.log('Live Nextcloud: acceptance, claimed waiting across polls/reload, independent dialog, completion PASS');
 } finally {
  if (own.length) await call('post','/bridge',{snapshot:old.snapshot,results:own.map(r => ({id:r.id,outcome:'Spinner verification finished'}))});
  else await call('post','/bridge',{snapshot:old.snapshot,results:[]});
  if (codex && !codexFinished) codex.kill();
  await browser.close(); await client.dispose();
  const path = require('path');
  assert.equal(path.dirname(path.resolve(isolated)), path.resolve(require('os').tmpdir()));
  assert.ok(path.basename(isolated).startsWith('cq-spinner-'));
  require('fs').rmSync(isolated,{recursive:true,force:true});
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
