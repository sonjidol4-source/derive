const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {todayReference,runs}=require('../chart_recovery.js');
const at=t=>'2026-10-30T'+t+':30+09:00';
const original=(t,v)=>({at:at(t),t,foreign:{fut_eq:v,opt_bias:v===null?null:-25}});
const archived=(t,v)=>({at:at(t),values:{foreign_fut_qty:v,foreign_spot_krx_amt:-900}});
const V={day:'2026-10-30',series:[original('11:53',0),original('12:29',-800)]};
const R={day:V.day,history:[archived('11:53',999),archived('11:54',-1000),archived('11:55',-990),archived('12:28',-810),archived('12:29',999)]};

test('upper chart gets actual general-futures reference inside the KIS gap; valid zero is preserved',()=>{
  const before=JSON.stringify([V,R]),ref=todayReference(V,R);
  assert.deepEqual(ref.points.map(p=>p.v),[-1000,-990,-810]);
  assert.equal(ref.minutes,3);assert.deepEqual(ref.ranges,[['11:54','11:55'],['12:28','12:28']]);
  assert.equal(JSON.stringify([V,R]),before);
  assert(ref.points.every(p=>!('fut_eq' in p)&&!('opt_bias' in p)));
});

test('a missing mini-inclusive sum can show a separate reference without manufacturing the sum',()=>{
  const view={...V,series:[V.series[0],original('11:54',null),V.series[1]]};
  const ref=todayReference(view,R);assert.equal(ref.points[0].v,-1000);
  assert.equal(view.series[1].foreign.fut_eq,null);assert.equal(view.series[1].foreign.opt_bias,null);
});

test('other trading days and times outside the original chart are excluded',()=>{
  assert.equal(todayReference(V,{...R,day:'2026-10-29'}).minutes,0);
  assert.equal(todayReference(V,{...R,history:[archived('08:46',20),archived('14:30',40)]}).minutes,0);
  assert.equal(todayReference({day:V.day,series:[]},R).minutes,0);
});

test('real time gaps, missing values and time reversal break paths, preserving isolated points',()=>{
  const p=[{at:at('10:00'),v:1},{at:at('10:01'),v:2},{at:at('10:02'),v:null},
    {at:at('10:03'),v:3},{at:at('10:20'),v:4},{at:at('10:19'),v:5}];
  assert.deepEqual(runs(p).map(r=>r.map(p=>p.v)),[[1,2],[3],[4],[5]]);
});

function renderer(){
  const elements=new Map();
  const make=()=>({attrs:{},children:[],setAttribute(k,v){this.attrs[k]=String(v)},append(n){this.children.push(n)},
    set innerHTML(v){this.children=[];this.html=v},get innerHTML(){return this.html||''}});
  const document={getElementById(id){if(!elements.has(id))elements.set(id,make());return elements.get(id)},createElementNS(){return make()}};
  const source=fs.readFileSync(path.join(__dirname,'../dashboard.html'),'utf8');
  const context=vm.createContext({document,RecoveryCharts:{todayReference,runs},setTimeout(){},
    renderTodayCum(){},renderTodayVol(){},renderTodayWeekly(){},renderTodayOI(){}});
  vm.runInContext(source.slice(source.indexOf('const $=id'),source.indexOf('function renderFlowExplanation')),context);
  vm.runInContext(source.slice(source.indexOf('function renderToday(V,'),source.indexOf('function renderTodayCum')),context);
  return {context,elements,flatten(id){const visit=n=>[n,...n.children.flatMap(visit)];return visit(document.getElementById(id))}};
}

test('actual chart renderer draws a single recovered minute visibly instead of an invisible M-only path',()=>{
  const ui=renderer();ui.context.chart('chart',[{color:'purple',recovered:true,points:[{at:at('12:45'),v:33}]}]);
  const dots=ui.flatten('chart').filter(n=>n.attrs['data-provenance']==='recovered'&&n.attrs.r);
  assert.equal(dots.length,1);assert.match(dots[0].children[0].textContent,/12:45/);
  assert(!ui.flatten('chart').some(n=>Object.values(n.attrs).some(v=>/NaN|Infinity/.test(v))));
});

test('actual upper-chart render includes recovery paths and explains the unfilled mini/option gap',()=>{
  const ui=renderer();ui.context.renderToday({...V,investors:[],source:'KIS'},R);
  const nodes=ui.flatten('today-chart'),path=nodes.find(n=>n.attrs['stroke-dasharray']==='5 4');
  assert(path.attrs.d.includes('L'));assert.equal((path.attrs.d.match(/M/g)||[]).length,2);
  assert.equal(nodes.filter(n=>n.attrs.r&&n.attrs['data-provenance']==='recovered').length,3);
  assert.equal(ui.elements.get('today-recovery-legend').hidden,false);
  assert.match(ui.elements.get('today-recovery-note').textContent,/미니 포함 합계·옵션.*아직 복구되지/);
});

// 2026-11-05: LS t1617 사후 복구(일반+미니×0.2, 월물+미니 옵션)는 같은 기준이라 노란·초록 점선으로 그린다.
const kisPoint=(t,fut,opt)=>({at:at(t),t,foreign:{fut_eq:fut,opt_partial:opt}});
const RK={...R,kis:{day:V.day,points:[kisPoint('08:50',-100,-3),kisPoint('11:53',5,1),kisPoint('11:54',-1972,-40),kisPoint('11:55',-1960,null)]}};

test('t1617 recovery fills same-basis gaps (incl. before first record) and replaces the general-futures reference there',()=>{
  const {kisReference,todayReference}=require('../chart_recovery.js');
  const k=kisReference(V,RK);
  assert.deepEqual(k.fut.map(p=>p.v),[-100,-1972,-1960]);   // 11:53은 한투 원래 값이 있어 제외
  assert.deepEqual(k.opt.map(p=>p.v),[-3,-40]);
  assert.deepEqual(k.ranges,[['08:50','08:50'],['11:54','11:55']]);
  const ref=todayReference(V,RK);
  assert.deepEqual(ref.points.map(p=>p.v),[-810]);           // 11:54·11:55 보라 참고값은 같은 기준 값으로 대체
  assert.equal(kisReference(V,{...RK,kis:{...RK.kis,day:'2026-11-04'}}).minutes,0);
});

test('upper chart draws t1617 recovery as gold and green dashed lines with an explanation',()=>{
  const ui=renderer();ui.context.renderToday({...V,investors:[],source:'KIS'},RK);
  const dashed=ui.flatten('today-chart').filter(n=>n.attrs['stroke-dasharray']==='5 4');
  assert(dashed.some(n=>n.attrs.stroke==='var(--gold)'));assert(dashed.some(n=>n.attrs.stroke==='var(--green)'));
  assert.match(ui.context.document.getElementById('today-recovery-note').textContent,/t1617/);
  assert.match(ui.context.document.getElementById('today-recovery-note').textContent,/위클리/);
});
