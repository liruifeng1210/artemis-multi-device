'use strict';
/* Artemis prototype: definitions, immutable run snapshots and local simulation. */
window.Artemis = (() => {
  const KEY = 'artemis-unified-v1';
  const clone = value => JSON.parse(JSON.stringify(value));
  const modules = ['账号与登录', '会议体验', '音视频', '消息与通知', '未分类'];
  const devices = {Android:['Pixel 8 · Android 15','Galaxy S24 · Android 14'],iOS:['iPhone 16 · iOS 18'],macOS:['QA Mac mini'],Windows:['QA Windows desktop'],Linux:['QA Linux desktop']};
  const defaults = {platform:'Android',device:devices.Android[0],version:'6.6.0',build:'45121',environment:'Staging',account:'tester@example.com',accountMode:'system',tool:'HappyCoding',mode:'auto',model:'系统默认',batch:'',recording:'always',logs:'always'};
  const plan = ['打开登录页','输入邮箱','进入密码页','完成登录','验证首页'];
  function makeSteps(email) {
    return [
      {name:plan[0],status:'pending',expected:'打开邮箱登录页，邮箱输入框与下一步按钮可见。',actual:'等待执行',before:null,after:null,events:[]},
      {name:plan[1],status:'pending',expected:`邮箱输入框显示 ${email}。`,actual:'等待执行',before:null,after:null,events:[]},
      {name:plan[2],status:'pending',expected:'点击下一步后进入密码页，密码输入框可见。',actual:'等待执行',before:null,after:null,events:[]},
      {name:plan[3],status:'pending',expected:'使用配置的测试账号完成登录，离开密码页。',actual:'等待执行',before:null,after:null,events:[]},
      {name:plan[4],status:'pending',expected:'首页可见，账号状态与本次登录账号一致。',actual:'等待执行',before:null,after:null,events:[]}
    ];
  }
  const script = [
    {step:0,type:'action',status:'sent',text:'已发送打开登录页动作，等待页面校验。',before:null,after:null,tool:'open_login_page()'},
    {step:0,type:'validation',status:'verified',text:'已观察到邮箱输入框与下一步按钮。',after:'empty',tool:'assert_visible("email", "next") → true'},
    {step:1,type:'action',status:'sent',text:'已发送邮箱输入动作。',before:'empty',after:'empty',tool:'focus_and_input_text("email", account)'},
    {step:1,type:'validation',status:'verified',text:'邮箱输入内容与本次测试账号一致。',after:'email',tool:'assert_text("email", account) → true'},
    {step:2,type:'action',status:'sent',text:'首次点击下一步，等待密码页。',before:'email',after:'email',tool:'tap("next") → dispatched'},
    {step:2,type:'validation',status:'failed',text:'验证未通过：仍停留在邮箱页，未发现密码输入框。',after:'email',tool:'assert_visible("password") → false'},
    {step:2,type:'retry',status:'sent',text:'等待后重试点击下一步；保留首次失败证据。',after:'email',tool:'wait_for_idle(); tap("next") → dispatched'},
    {step:2,type:'validation',status:'recovered',text:'重试后密码输入框可见，本阶段已恢复。',after:'password',tool:'assert_visible("password") → true'},
    {step:3,type:'action',status:'sent',text:'已填写脱敏凭据并发送登录动作。',before:'password',after:'password-filled',tool:'fill_secret("password", "[REDACTED]"); tap("login")'},
    {step:3,type:'validation',status:'verified',text:'已离开密码页，观察到已登录会话。',after:'home',tool:'assert_authenticated() → true'},
    {step:4,type:'action',status:'sent',text:'开始核对首页内容与当前账号。',before:'home',after:'home',tool:'inspect_home_and_account()'},
    {step:4,type:'validation',status:'verified',text:'首页与账号校验通过，全部测试目标完成。',after:'home',tool:'assert_home_visible() && assert_account(account) → true'}
  ];
  function applyEvent(run, index, at) {
    const entry = script[index];
    const step = run.steps[entry.step];
    if ('before' in entry) step.before = entry.before;
    step.after = entry.after;
    step.status = entry.status;
    step.actual = entry.text;
    step.at = at;
    if (index === 5) step.issue = {at,before:'email',after:'email',expected:step.expected,actual:entry.text};
    if (index === 6) run.adjustments.push({at,text:'首次点击未进入密码页，增加等待与一次重试。',step:2});
    if (index === 7) step.recovery = {at,after:'password',text:entry.text};
    const actionIndex=index===1?0:index===3?2:index===5?4:index===7?6:index===9?8:index===11?10:null;
    step.events.push({id:`${run.id}-e${index}`,at,type:entry.type,status:entry.status,text:entry.text,tool:entry.tool,frame:entry.after,step:entry.step,actionId:actionIndex===null?null:`${run.id}-e${actionIndex}`,decisionFrame:null,source:'prototype-script'});
    if(index===6) step.events.push({id:`${run.id}-output-retry`,at:at-1,type:'output',text:'仍停留在邮箱页。等待后再次点击“下一步”。',source:'legacy-example',decisionFrame:null,callId:null,step:entry.step,tool:'原执行详情 logData 中的示例执行摘要；非真实模型调用记录。'});
    run.cursor = index + 1;
    run.current = entry.step;
    for (const instruction of run.instructions) if (instruction.state === 'pending') {
      instruction.state = 'recorded'; instruction.processedAt = at; instruction.step = entry.step;
      step.events.push({id:instruction.id,at,type:'instruction',text:`补充指令已记录，采纳状态未确认。`,tool:'instruction_received · adoption_unconfirmed',step:entry.step,frame:null});
    }
    if (index === script.length - 1) { run.status = 'passed'; run.ended = at; }
  }
  function seededRun(c, number, time, outcome) {
    const run = {id:`RUN-${String(number).padStart(4,'0')}`,caseId:c.id,snapshot:clone(c),config:clone(c.config),started:time,ended:null,status:'running',cursor:0,current:0,steps:makeSteps(c.config.account),instructions:[],adjustments:[],connectionEvents:[],sample:true};
    delete run.snapshot.runs;
    const offsets=[0,3,5,8,12,14,18,23,28,34,40,46];
    const count=outcome==='passed'?12:8;
    for(let i=0;i<count;i++) applyEvent(run,i,time+offsets[i]*1000);
    if(outcome!=='passed'){
      run.status=outcome;run.ended=time+72000;run.current=3;
      const s=run.steps[3];s.status='insufficient';s.before='password';s.after='password';s.at=run.ended;
      s.actual=outcome==='stopped'?'用户停止执行，登录和首页目标未完成。':'结束时仍停留在密码页，缺少登录成功证据。';
      s.events.push({id:run.id+'-end',at:run.ended,type:'validation',status:'insufficient',text:s.actual,tool:'assert_authenticated() → unconfirmed; home_check → not_run',frame:'password',step:3});
      run.steps[4].actual='尚未执行，首页未验证。';
    }
    return run;
  }
  function seed(){
    const seedDay=new Date();seedDay.setDate(seedDay.getDate()-1);seedDay.setHours(14,32,0,0);const base=seedDay.getTime();
    const main={id:'CASE-024',name:'验证邮箱登录流程',objective:'使用邮箱完成登录；首次点击下一步未进入密码页时等待并重试，确认密码页、登录会话与首页。',description:'前置条件：测试应用已安装，账号可用且未登录。使用 tester@example.com，凭据由运行环境提供。保留异常前后画面，不将动作已发送等同于验证通过。',module:'账号与登录',tags:['冒烟','核心路径'],owner:'我',archived:false,control:'',updated:base+120000,revision:3,config:clone(defaults),notes:[],scenario:'email-login'};
    const examples=[
      ['CASE-023','验证退出登录后的会话清理','账号与登录',['回归'],'我'],
      ['CASE-022','验证会议邀请链接入会','会议体验',['冒烟'],'陈悦'],
      ['CASE-021','验证摄像头权限提示','音视频',['权限'],'我'],
      ['CASE-020','验证网络恢复后的消息发送','消息与通知',['回归','网络'],'我'],
      ['CASE-019','验证麦克风切换','音视频',['回归'],'林远'],
      ['CASE-018','验证后台通知打开会话','消息与通知',['冒烟'],'我'],
      ['CASE-017','验证旧版邮箱登录入口','账号与登录',['回归'],'我']
    ].map((x,i)=>({id:x[0],name:x[1],objective:x[1]+'，检查预期行为并记录关键证据。',description:'示例测试定义，尚未配置对应的自动执行脚本。',module:x[2],tags:x[3],owner:x[4],archived:i===6,control:i===2?'另一台工作站':'',updated:base-(i+1)*3600000,revision:1,config:clone(defaults),notes:[],scenario:'custom'}));
    return {version:1,cases:[main,...examples],runs:[seededRun(main,18,base,'failed'),seededRun(main,17,base-86400000,'passed'),seededRun(main,16,base-172800000,'stopped')],nextCase:25,nextRun:19};
  }
  let storageAvailable=true, data;
  try { data=JSON.parse(localStorage.getItem(KEY)); if(!data||data.version!==1||!Array.isArray(data.cases)||!Array.isArray(data.runs)) data=seed(); }
  catch { data=seed();storageAvailable=false; }
  // Local simulated work only advances while this page is open.
  for(const r of data.runs) if(r.status==='running') {r.status='waiting';r.resumeRequired=true;}
  // Additive migration: retain all original run IDs, snapshots, timestamps and results.
  for(const c of data.cases){c.notes ||= [];c.tags ||= [];c.pending ||= null;}
  for(const r of data.runs){
    const explicitPairs={1:0,3:2,5:4,7:6,9:8,11:10};
    for(const step of r.steps) for(const event of step.events){
      const prefix=r.id+'-e';
      if(event.type==='validation'&&event.id?.startsWith(prefix)&&event.actionId===undefined){
        const index=Number(event.id.slice(prefix.length));
        if(explicitPairs[index]!==undefined)event.actionId=prefix+explicitPairs[index];
      }
    }
  }
  data.workspaceVersion=2;
  const deviceCatalog=[
    {id:'pixel-8',name:'Pixel 8',platform:'Android',detail:'Android 15 · 本地设备',state:'ready',configName:devices.Android[0]},
    {id:'iphone-16',name:'iPhone 16',platform:'iOS',detail:'iOS 18 · 本地设备',state:'busy',configName:devices.iOS[0]},
    {id:'galaxy-s24',name:'Galaxy S24',platform:'Android',detail:'Android 14 · 本地设备',state:'offline',configName:devices.Android[1]}
  ];
  function save(){try{localStorage.setItem(KEY,JSON.stringify(data));storageAvailable=true;return true;}catch{storageAvailable=false;return false;}}
  function getCase(id){return data.cases.find(c=>c.id===id);}
  function getRun(id){return data.runs.find(r=>r.id===id);}
  function runsFor(id){return data.runs.filter(r=>r.caseId===id).sort((a,b)=>b.started-a.started||b.id.localeCompare(a.id));}
  function active(id){return runsFor(id).find(r=>['running','waiting'].includes(r.status));}
  function start(c,config){
    const snapshot=clone(c);
    const run={id:`RUN-${String(data.nextRun++).padStart(4,'0')}`,caseId:c.id,snapshot,config:clone(config),started:Date.now(),ended:null,status:config.mode==='step'?'waiting':'running',cursor:0,current:0,steps:makeSteps(config.account),instructions:[],adjustments:[],connectionEvents:[],sample:true,custom:c.scenario!=='email-login'};
    if(run.custom){run.steps=[{name:'准备测试目标',status:'pending',expected:c.objective||'明确测试目标',actual:'该自定义 Case 尚无演示执行脚本。',before:null,after:null,events:[]}];run.status='waiting';}
    data.runs.unshift(run);save();return run;
  }
  function advance(run,manual=false){
    if(!run||run.custom||!['running','waiting'].includes(run.status))return;
    if(run.status==='waiting'&&!manual)return;
    run.resumeRequired=false;run.status='running';
    applyEvent(run,run.cursor,Date.now());
    if(run.status==='running'&&run.config.mode==='step')run.status='waiting';
    save();
  }
  function stop(run){
    if(!run||!['running','waiting'].includes(run.status))return;
    run.status='stopped';run.ended=Date.now();
    const s=run.steps[run.current];
    if(s.status==='sent'){s.status='insufficient';s.actual='执行已停止，本动作的结果尚未验证。';}
    s.events.push({id:run.id+'-stop',at:run.ended,type:'control',text:'用户停止本次运行；保留已记录的事件与证据。',tool:'stop_requested → stopped',step:run.current,frame:s.after});save();
  }
  function saveCase(values,id){
    let c=getCase(id);
    if(c){Object.assign(c,clone(values));c.revision++;c.updated=Date.now();}
    else {const text=values.name+' '+values.objective;c={...clone(values),id:`CASE-${String(data.nextCase++).padStart(3,'0')}`,owner:'我',archived:false,control:'',revision:1,updated:Date.now(),notes:[],scenario:/邮箱/.test(text)&&/登录/.test(text)?'email-login':'custom'};data.cases.unshift(c);}
    save();return c;
  }
  function submitIntent(c,text){
    if(active(c.id))return null;
    c.pending={id:'intent-'+Date.now(),at:Date.now(),text,snapshot:clone({...c,pending:null}),deviceId:deviceCatalog[0].id};
    save();return c.pending;
  }
  function startPending(c,deviceId){
    const pending=c.pending,device=deviceCatalog.find(d=>d.id===deviceId);
    if(!pending||!device||device.state!=='ready'||active(c.id))return null;
    const definition=clone(pending.snapshot);definition.objective=pending.text;
    definition.scenario=/邮箱/.test(definition.name+' '+pending.text)&&/登录/.test(definition.name+' '+pending.text)?'email-login':'custom';
    const config={...clone(defaults),platform:device.platform,device:device.configName};
    const run=start(definition,config);
    run.intent={id:pending.id,at:pending.at,text:pending.text};run.deviceId=device.id;
    run.planVersions=[];c.pending=null;save();return run;
  }
  function addInstruction(run,text){if(!run||!['running','waiting'].includes(run.status))return false;run.instructions.push({id:run.id+'-instruction-'+(run.instructions.length+1),at:Date.now(),text,state:'pending'});save();return true;}
  return {data,defaults,devices,deviceCatalog,modules,plan,clone,getCase,getRun,runsFor,active,save,start,advance,stop,saveCase,submitIntent,startPending,addInstruction,get storageAvailable(){return storageAvailable;}};
})();
