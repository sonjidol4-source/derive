const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../dashboard.html'),'utf8');
const code=html.slice(html.indexOf('let sessionLoad=null;'),html.indexOf('function importCSV'));
const reply=(status,body)=>({status,ok:status>=200&&status<300,json:async()=>body});
function client(fetcher,initial='old-test-session'){
  const calls=[];
  const context=vm.createContext({fetch:async(url,options)=>{calls.push({url,options});return fetcher(url,options,calls.length)}});
  vm.runInContext('let token='+JSON.stringify(initial)+';'+code,context);
  return {calls,request:context.request};
}
test('stale session is renewed once, then the original recovery request succeeds with identical data',async()=>{
  const c=client((url,options,n)=>n===1?reply(401,{code:'session_expired'}):n===2?reply(200,{token:'new-test-session'}):reply(200,{accepted:true}));
  assert.equal((await c.request('/api/recovery',{ranges:[['11:53','12:29']]})).accepted,true);
  assert.equal(c.calls.length,3);assert.equal(c.calls[1].url,'/api/bootstrap');
  assert.equal(c.calls[1].options.cache,'no-store');
  assert.equal(c.calls[0].options.headers.Authorization,'Bearer old-test-session');
  assert.equal(c.calls[2].options.headers.Authorization,'Bearer new-test-session');
  assert.equal(c.calls[0].options.body,c.calls[2].options.body);
});
test('old server authentication failure is handled during a rolling application update',async()=>{
  const c=client((url,options,n)=>n===1?reply(403,{error:'인증 실패'}):n===2?reply(200,{token:'fresh'}):reply(200,{accepted:true}));
  assert.equal((await c.request('/api/recovery',{})).accepted,true);assert.equal(c.calls.length,3);
});
test('initial clicks wait for one shared bootstrap instead of sending an empty credential',async()=>{
  let release;
  const c=client(url=>url==='/api/bootstrap'?new Promise(resolve=>{release=()=>resolve(reply(200,{token:'ready'}))}):reply(200,{accepted:true}),'');
  const first=c.request('/api/recovery',{}),second=c.request('/api/recovery',{});
  assert.equal(c.calls.length,1);release();await Promise.all([first,second]);
  assert.equal(c.calls.filter(c=>c.url==='/api/bootstrap').length,1);
  assert(c.calls.filter(c=>c.url!=='/api/bootstrap').every(c=>c.options.headers.Authorization==='Bearer ready'));
});
test('a failed session refresh stops before retrying the operation',async()=>{
  const c=client((url,options,n)=>n===1?reply(401,{code:'session_expired'}):reply(503,{error:'offline'}));
  await assert.rejects(c.request('/api/recovery',{}),/새로고침/);assert.equal(c.calls.length,2);
});
test('origin and preview restrictions never cause an authentication retry',async()=>{
  for(const body of [{code:'origin_rejected',error:'local only'},{error:'저장본 미리보기'}]){
    const c=client(()=>reply(403,body));await assert.rejects(c.request('/api/recovery',{}));assert.equal(c.calls.length,1);
  }
});
test('another rejection after refresh is returned without a retry loop',async()=>{
  const c=client(url=>url==='/api/bootstrap'?reply(200,{token:'still-stale'}):reply(401,{code:'session_expired',error:'session changed again'}));
  await assert.rejects(c.request('/api/recovery',{}),/session changed again/);assert.equal(c.calls.length,3);
});
test('uncertain network failure is not replayed',async()=>{
  const c=client(()=>{throw Error('connection lost')});
  await assert.rejects(c.request('/api/recovery',{}),/connection lost/);assert.equal(c.calls.length,1);
});
test('ordinary validation errors do not refresh or replay',async()=>{
  const c=client(()=>reply(400,{error:'bad range'}));
  await assert.rejects(c.request('/api/recovery',{}),/bad range/);assert.equal(c.calls.length,1);
});
test('a valid session performs the operation exactly once',async()=>{
  const c=client(()=>reply(200,{accepted:true}));
  assert.equal((await c.request('/api/recovery',{})).accepted,true);assert.equal(c.calls.length,1);
});
