/* Review UI uses saved records; no writes or broker calls. */
let reviewView='auto';
const reviewStyle=document.createElement('style');
reviewStyle.textContent=`.review-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px}.review-toolbar .tabs{margin:0}.review-scope{color:var(--muted);font-size:12px;line-height:1.7}.review-summary{font-size:14px;line-height:1.8}.review-mode-close #turns,.review-mode-close #confirm{display:none}.review-method{margin:12px 0;padding:12px;border-left:2px solid var(--gold);line-height:1.8;font-size:12px}.review-stats td{min-width:100px}.review-stats td:first-child{min-width:180px}@media(max-width:1350px){header nav{display:none}}`;
document.head.appendChild(reviewStyle);
document.querySelector('.hero').insertAdjacentHTML('beforebegin',`<div class="review-toolbar"><div><b>보기 순서</b><div id="review-source" class="review-scope"></div></div><div class="tabs" id="view-tabs"><button data-view="auto">자동</button><button data-view="intraday">장중 관측</button><button data-view="close">마감 복기</button></div></div>`);
document.getElementById('close').insertAdjacentHTML('afterend',`<section class="panel" id="cross-check"><div class="panel-head"><h2>한투 · LS 마감 대조</h2><span id="cross-status" class="chip"></span></div><div id="cross-scope" class="review-scope"></div><div id="cross-table" class="scroller"></div><div id="cross-note" class="note"></div></section>`);
document.getElementById('events').insertAdjacentHTML('beforebegin',`<section class="panel" id="validation"><div class="panel-head"><h2>관측 유형별 결과 · 검증 준비 상태</h2><select id="validation-horizon" aria-label="평가 기간"><option value="5">5분 뒤</option><option value="15" selected>15분 뒤</option><option value="30">30분 뒤</option><option value="60">60분 뒤</option></select></div><div id="validation-audit" class="review-summary"></div><div id="validation-note" class="note"></div><details style="margin-top:12px" open><summary>유형·규칙·자료 출처별 집계</summary><div id="validation-table" class="scroller"></div></details></section>`);
document.getElementById('view-tabs').onclick=e=>{if(e.target.dataset.view){reviewView=e.target.dataset.view;if(lastState)render(lastState)}};
document.getElementById('validation-horizon').onchange=()=>lastState&&renderValidation(lastState.review?.validation);
document.querySelector('.review-toolbar').insertAdjacentHTML('afterend','<div class="legend" style="margin-bottom:16px;font-size:12px"><a href="#rebound">반등 감지</a><a href="#gap-recovery">공백 복구</a><a href="#close">마감 요약</a><a href="#cross-check">증권사 대조</a><a href="#today">전체 파생 수급</a><a href="#validation">유형별 검증</a><a href="#connection">수신 상태</a></div>');
const reviewOrigin=p=>({observed:'수집 당시 관측',recovered:'사후 복구',manual:'수동 입력',mixed:'출처 혼합',unknown:'미기록',demo:'가상'}[p]||p||'미기록');
function renderValidation(V){
 if(!V){$('validation-audit').textContent='집계 자료가 없습니다.';return}
 const a=V.audit||{},h=$('validation-horizon').value;
 $('validation-audit').textContent=`저장 관측 ${fmt(V.total)}건 · 규칙 상세 미기록 ${fmt(a.missing_rule_hash)}건 · 입력 출처 미기록 ${fmt(a.missing_provenance)}건`;
 $('validation-note').textContent=V.note+' 과거 자료에 없는 메타정보는 소급해서 만들지 않습니다. 앞으로의 관측부터 기록합니다. 양수 비율은 방향수익률이 0보다 큰 비율이며 예측 확률이 아닙니다.';
 $('validation-table').innerHTML='<table class="review-stats"><thead><tr><th>관측 종류 / 투자자</th><th>규칙 버전 / 자료 출처</th><th>관측 / 거래일</th><th>평가 가능 / 누락</th><th>평가 거래일 / 겹침</th><th>방향수익률 평균</th><th>양수 비율</th><th>평가가격 출처별</th></tr></thead><tbody>'+V.groups.map(g=>{const s=g.horizons[h];return '<tr><td>'+esc(g.kind)+'<div class="note">'+esc(g.investor==='unspecified'?'투자자 미기록':g.investor)+'</div></td><td>'+esc(g.rule_version)+'<div class="note">'+esc(g.rule_hash==='unknown'?'규칙 상세 미기록':g.rule_hash)+'<br>'+esc(reviewOrigin(g.provenance))+' · '+esc(g.mode)+'</div></td><td>'+fmt(g.count)+' / '+fmt(g.days)+'일</td><td>'+fmt(s.valid)+' / '+fmt(s.missing)+'</td><td>'+fmt(s.days)+'일 / '+fmt(s.overlap_count)+'건</td><td>'+signed(s.mean_directional_pct,3)+'%</td><td>'+fmt(s.positive_pct,1)+'%</td><td>'+Object.entries(s.by_provenance||{}).map(([k,x])=>esc(reviewOrigin(k))+': '+fmt(x.valid)+'건 · '+signed(x.mean_directional_pct,3)+'%').join('<br>')+'</td></tr>'}).join('')+'</tbody></table>';
}
function renderReview(D){
 const C=D.close_summary,L=C?.latest,R=C?.rows?.[0],closed=reviewView==='close'||(reviewView==='auto'&&!D.session&&D.mode!=='demo');
 document.body.classList.toggle('review-mode-close',closed);
 document.querySelectorAll('#view-tabs button').forEach(b=>b.classList.toggle('active',b.dataset.view===reviewView));
 $('review-source').textContent=(D.mode==='preview'?'저장본 미리보기 · API 연결 없음':(D.mode==='live'||D.session?'실시간 수집':'수집 중 · 장외'))+' · 화면 시각 '+String(D.generated_at||'').replace('T',' ').slice(0,19)+' KST';
 const today=$('today'),anchor=closed?today:$('insight');
 anchor.before($('close'));anchor.before($('cross-check'));
 document.querySelector('.hero .eyebrow').textContent=closed?'DAILY REVIEW · 마감 자료 기준':'INTRADAY FLOW · 최근 5분 판정';
 document.querySelector('.context-side .eyebrow').textContent=closed?'장중 관측 배경 · 기준 시각 확인':'당일 배경 · 관측값';
 if(D.mode==='preview'){
   $('mode').textContent='저장본 미리보기';
   $('banner').textContent='API에 연결하지 않은 저장본 미리보기입니다. '+(L?L.day+' 거래일 저장 자료를 복기합니다.':'저장 자료를 표시합니다.')+' 화면 조작은 기록을 변경하지 않습니다.';
   document.querySelectorAll('button[onclick]').forEach(b=>{if(/importCSV|saveSources|submitManual/.test(b.getAttribute('onclick')))b.disabled=true});
 }
 if(closed&&L&&R){
   $('verdict').textContent=L.day+' 마감 복기';$('verdict').className='';
   $('guide').textContent=L.text;
   $('facts').innerHTML=['일반선물: LS t1602 · '+L.status+' · 마지막 변화 '+L.last_change,
     '미니 포함 수량은 한투 미니 × 0.2를 더한 값입니다.',
     '옵션 금액: 월물·위클리·미니 합산과 월물 단독을 구분합니다.'].map(t=>'<li>'+esc(t)+'</li>').join('');
   const v=[['외국인 일반선물',R.foreign_qty,0,'계약 · LS 마감'],['외국인 미니 포함',R.foreign_eq?.qty,0,'일반 + 미니 × 0.2 · 계약'],['기관 일반선물',R.inst_qty,0,'계약 · LS 마감'],['선물 종가',R.fut_price,2,'포인트 · '+(L.futures?.observed_at||'시각 미기록')],['시장 미결제 증감',R.oi_change,0,'계약 · 투자자 보유량 아님'],['외국인 현물',R.foreign_spot,0,'억원 · '+(R.spot_note||'저장 관측')]];
   $('cards').innerHTML=v.map(([title,value,d,n])=>'<div class="card"><div class="title">'+esc(title)+'</div><strong class="num '+(title==='선물 종가'?'':color(value))+'">'+(title==='선물 종가'?fmt(value,d):signed(value,d))+'</strong><small>'+esc(n)+'</small></div>').join('');
 }else if(!D.session&&reviewView==='intraday'){
   $('verdict').textContent='장중 마지막 관측';$('verdict').className='';$('guide').textContent='당시 관측을 살펴보는 화면입니다. 비어 있는 5·15분 값은 마감 자료로 대신 채우지 않습니다.';
 }
 if(L){
   let m=$('close-method');if(!m){m=document.createElement('div');m.id='close-method';m.className='review-method';$('close-options').after(m)}
   const current=L.recalculated_options;
   m.innerHTML='<b>'+esc(L.option_method_note)+'</b><div>'+esc(L.structure_text||'저장 옵션 문장 없음')+'</div>'+(current?'<div style="margin-top:8px"><b>'+esc(current.label)+' · ±'+esc(current.methodology?.near_band_pct??'미기록')+'%</b><br>'+esc(current.text)+'</div>':'');
   // Keep saved text in one place with its methodology label.
   $('close-options').innerHTML=(L.option_lines||[]).map(t=>'<div class="note">옵션 · '+esc(t)+'</div>').join('');
   document.querySelectorAll('#close-table tbody tr').forEach((tr,i)=>{const cell=tr.cells[0];if(cell)cell.insertAdjacentHTML('beforeend','<div class="note">'+esc(C.rows[i].option_method_note)+'</div>')});
 }
 const X=D.review?.comparison;
 if(X){
   $('cross-status').textContent=({matched:'4개 투자자 일치',different:'차이 있음',partial:'일부 누락',unavailable:'대조 대기'}[X.status]||X.status);
   $('cross-scope').textContent=(X.day||'거래일 미확인')+' · '+X.scope+' · LS 자료 '+(X.ls_data_at||'없음')+' / 조회 '+(X.ls_at||'없음')+' · 한투 조회 '+(X.kis_at||'없음');
   $('cross-table').innerHTML='<table><thead><tr><th>투자자</th><th class="r">LS</th><th class="r">한투</th><th class="r">한투 − LS</th></tr></thead><tbody>'+X.rows.map(r=>'<tr><td>'+esc(r.label)+'</td><td class="r num">'+signed(r.ls_qty)+'</td><td class="r num">'+signed(r.kis_qty)+'</td><td class="r num">'+signed(r.difference)+'</td></tr>').join('')+'</tbody></table>';
   $('cross-note').textContent=X.note;
 }
 renderValidation(D.review?.validation);
 $('eventstats').innerHTML='<span class="pill">최근 관측 목록 '+D.events.length+'건</span><span class="pill">전체 유형별 결과는 위 집계 표 참조</span>';
}
