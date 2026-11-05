/* Display-only recovery helpers. No changes to live observations or signals. */
(function(root,factory){
  if(typeof module==='object'&&module.exports)module.exports=factory();
  else root.RecoveryCharts=factory();
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  const valid=v=>typeof v==='number'&&Number.isFinite(v);

  function spans(points){
    const ranges=[];
    for(const p of points){
      const tail=ranges[ranges.length-1];
      if(tail&&Date.parse(p.at)-Date.parse(tail[1])<=60000)tail[1]=p.at;
      else ranges.push([p.at,p.at]);
    }
    return ranges.map(r=>r.map(t=>t.slice(11,16)));
  }

  // LS t1617 사후 복구(일반+미니×0.2, 월물+미니 옵션). 한투 원래 값이 있는 분은 쓰지 않는다.
  function kisReference(view,recovery){
    const empty={fut:[],opt:[],minutes:0,ranges:[]};
    const k=recovery?.kis;
    if(!view?.day||!k||k.day!==view.day)return empty;
    const occupied=new Set((view.series||[]).filter(p=>valid(p.foreign?.fut_eq)).map(p=>p.at.slice(0,16)));
    const pts=(k.points||[]).filter(p=>p.at?.slice(0,10)===view.day&&!occupied.has(p.at.slice(0,16)))
      .sort((a,b)=>Date.parse(a.at)-Date.parse(b.at));
    const fut=pts.filter(p=>valid(p.foreign?.fut_eq)).map(p=>({at:p.at,v:p.foreign.fut_eq}));
    const opt=pts.filter(p=>valid(p.foreign?.opt_partial)).map(p=>({at:p.at,v:p.foreign.opt_partial}));
    return {fut,opt,minutes:fut.length,ranges:spans(fut)};
  }

  function todayReference(view,recovery){
    const kis=kisReference(view,recovery);
    const empty={points:[],minutes:0,ranges:[],kis};
    if(!view?.day||view.day!==recovery?.day)return empty;
    const original=(view.series||[]).filter(p=>p.at?.slice(0,10)===view.day);
    if(!original.length)return empty;
    const first=Date.parse(original[0].at),last=Date.parse(original[original.length-1].at);
    // A general-futures value cannot replace the original mini-inclusive sum.
    // Show an independent reference only in minutes with a missing original sum.
    const occupied=new Set(original.filter(p=>valid(p.foreign?.fut_eq)).map(p=>p.at.slice(0,16)));
    for(const p of kis.fut)occupied.add(p.at.slice(0,16));  // 같은 기준 복구값이 있으면 일반선물 참고값은 그리지 않는다
    const points=(recovery.history||[])
      .filter(h=>h.at?.slice(0,10)===view.day&&Date.parse(h.at)>=first&&Date.parse(h.at)<=last
        &&!occupied.has(h.at.slice(0,16))&&valid(h.values?.foreign_fut_qty))
      .map(h=>({at:h.at,v:h.values.foreign_fut_qty})).sort((a,b)=>Date.parse(a.at)-Date.parse(b.at));
    return {points,minutes:points.length,ranges:spans(points),kis};
  }

  function runs(points,maxGap=150000){
    const result=[];let run=[];
    for(const p of points){
      const previous=run[run.length-1];
      const elapsed=previous&&p.at&&previous.at?Date.parse(p.at)-Date.parse(previous.at):0;
      if(!valid(p.v)||!Number.isFinite(elapsed)||elapsed<0||elapsed>maxGap){
        if(run.length)result.push(run);run=[];
      }
      if(valid(p.v))run.push(p);
    }
    if(run.length)result.push(run);
    return result;
  }
  return {todayReference,kisReference,runs};
});
