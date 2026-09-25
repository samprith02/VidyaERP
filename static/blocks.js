/* VidyaERP :: block renderers, shared by the Registrar's console (index.html)
   and the self-service portal (portal.html). Loaded locally - the console
   still fetches nothing from outside. Every string reaching innerHTML goes
   through esc() (or md(), which escapes first). */
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const md=s=>esc(s).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`(.+?)`/g,'<code>$1</code>')
   .replace(/_(.+?)_/g,'<em>$1</em>').replace(/\n\n/g,'</p><p>').replace(/\n/g,'<br>');

/* ---------------- block renderers ---------------- */
function rTable(b){
  const h=b.columns.map(c=>`<th>${esc(c)}</th>`).join('');
  const r=b.rows.map(row=>`<tr>${row.map(c=>`<td>${md(c)}</td>`).join('')}</tr>`).join('');
  return (b.title?`<h4 class="blk">${esc(b.title)}</h4>`:'')+
    `<div class="tblwrap ${b.dense?'dense':''}"><table><thead><tr>${h}</tr></thead><tbody>${r}</tbody></table></div>`;
}
function rCards(b){
  return (b.title?`<h4 class="blk">${esc(b.title)}</h4>`:'')+'<div class="cards">'+
    b.cards.map(c=>`<div class="card ${c.tone||''}"><div class="l">${esc(c.label)}</div>
      <div class="v">${esc(c.value)}</div>${c.sub?`<div class="s">${esc(c.sub)}</div>`:''}</div>`).join('')+'</div>';
}
function rNotice(b){
  return b.items.map(m=>`<div class="notice"><div class="a">${esc(m.audience)}</div>
    <div class="t">${esc(m.title)}</div><div class="b">${esc(m.body)}</div>
    <div class="ch">via ${esc(m.channel)}</div></div>`).join('');
}
function rPlans(b){
  return b.plans.map((p,i)=>{
    const legs=p.legs.map(l=>{
      const cls={SUBSTITUTE:'sub',SWAP:'swap',MAKEUP:'makeup',UNCOVERED:'unc',VACATED:'vacated'}[l.action];
      return `<div class="leg"><div class="p">P${l.period}<br><small style="color:#54648f">${l.time}</small></div>
        <div class="act"><span class="chipx ${cls}">${String(l.action).charAt(0)+String(l.action).slice(1).toLowerCase()}</span></div>
        <div class="d"><b>${esc(l.class)}</b> · ${esc(l.subject)} ${esc(l.subject_name||'')}
        <small>→ ${esc(l.to)} — ${esc(l.why)}</small></div></div>`}).join('');
    return `<div class="plan ${i===0?'best':''}">
      <div class="ph"><div class="pcode">${p.code}</div><b>${esc(p.title)}</b>
        ${i===0?'<span class="tag">Recommended</span>':''}</div>
      <div class="metrics">
        <div class="metric g"><div class="l">Confidence <span class="n">${p.confidence}%</span></div>
          <div class="bar"><i style="width:${p.confidence}%"></i></div></div>
        <div class="metric"><div class="l">Coverage${p.coverage_is_floor?' <small style="color:#98a2b3">(floor)</small>':''}
          <span class="n">${p.coverage}%</span></div>
          <div class="bar"><i style="width:${p.coverage}%"></i></div></div>
        <div class="metric"><div class="l">Continuity <span class="n">${p.continuity}</span></div>
          <div class="bar"><i style="width:${p.continuity}%"></i></div></div>
      </div>
      ${p.weights?`<div class="wts">ranked <b>${p.rank}</b> =
        ${p.weights.confidence}·confidence + ${p.weights.continuity}·continuity
        <span class="pol">weighting is a policy choice, not a measurement${
          p.coverage_is_floor
            ? ' · coverage is a feasibility floor, not a ranking axis: it has never changed an ordering, because an hour nobody can take scores zero on the other two axes as well'
            : ''}</span></div>`:''}
      ${p.also_commits&&p.also_commits.length?`<div class="wts">Plan
        ${p.also_commits.join(', ')} would commit exactly these same changes and
        ${p.also_commits.length>1?'are':'is'} not offered separately.</div>`:''}
      ${p.components?`<div class="wts">continuity measures —
        syllabus hours preserved <b>${p.components.hours_preserved}%</b> ·
        delivered by the subject's own teacher <b>${p.components.own_teacher}%</b> ·
        timing intact <b>${p.components.timing_intact}%</b></div>`:''}
      ${p.incomplete?`<div class="incomp">⚠ Incomplete — only <b>${p.coverage}%</b> of the affected
        subject-hours have a concrete arrangement. Ranking it first would not make it whole.</div>`:''}
      <div class="pb"><div style="color:var(--mut);font-size:12.5px;margin-bottom:7px">${esc(p.summary)}</div>
        <div class="legs">${legs}</div></div>
      <div class="pc"><div style="flex:1"><div class="h">Upside</div><ul>${p.pros.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>
        <div style="flex:1"><div class="h">Trade-off</div><ul>${p.cons.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div></div>
      <div class="pf"><button class="btn ${i===0?'ok':'ghost'}" onclick="say('apply plan ${p.code}')">Approve plan ${p.code}</button>
        <span>Nothing changes until you approve.</span></div>
    </div>`}).join('');
}
function rGrid(b){
  const per=Object.keys(b.periods).map(Number).sort((x,y)=>x-y);
  const br=b.breaks||{}; const dl=b.day_len||{};
  let h=`<div class="legend"><span><i style="background:#DCE2F4"></i>Lecture</span>
    <span><i style="background:#D3E9E1"></i>Lab</span>
    <span><i style="background:#E4E7EC"></i>Activity or project</span>
    <span><i style="background:#6A3DB8"></i>Changed for this date</span>
    <span><i style="background:#A35A06"></i>Make-up class</span></div>
    <div class="gridwrap"><table class="ttg"><thead><tr><th class="ph">Day</th>`;
  per.forEach(p=>{
    h+=`<th>P${p}<br><span style="color:var(--muted);font-weight:400">${b.periods[p]}</span></th>`;
    if(br[p]) h+=`<th class="brk"><span>${br[p].split('·')[0].trim()}</span></th>`;
  });
  h+='</tr></thead><tbody>';
  b.days.forEach(d=>{
    h+=`<tr><td class="dh">${d}</td>`;
    const last=dl[d]||per[per.length-1];
    per.forEach(p=>{
      const c=b.cells[d+'-'+p];
      if(!c){
        h+= p>last ? '<td class="over"><div class="cell off">day ends</div></td>' : '<td></td>';
      }else{
        const o=c.override;
        const k=(c.kind||'T');
        const cls=o?'ovr':k==='M'?'mk':k==='L'?'lab':k==='A'?'actv':'has';
        // a VACATED period is released: the absent teacher must not be left
        // standing against it, and a swapped-in subject shows what it replaced
        const who=o&&o.released?'Released':(o&&o.faculty?o.faculty:c.faculty);
        const was=o&&o.was?` (was ${esc(o.was)})`:'';
        const note=o?`, ${esc(o.action.toLowerCase())}`:k==='M'?`, make-up ${esc(c.makeup.date.slice(5))}`:'';
        h+=`<td><div class="cell ${cls}" title="${esc((o&&o.reason)||(c.makeup&&c.makeup.reason)||'')}"><div class="s">${esc(c.subject)}${was}</div>
          <div class="f">${esc(who)}</div>
          <div class="r">${esc(c.room)}${note}</div></div></td>`;
      }
      if(br[p]) h+=`<td class="brk"></td>`;
    });
    h+='</tr>'});
  return (b.title?`<h4 class="blk">${esc(b.title)}</h4>`:'')+h+'</tbody></table></div>';
}
const attr=s=>esc(s).replace(/"/g,'&quot;');
function rActions(b){
  return (b.title?`<h4 class="blk">${esc(b.title)}</h4>`:'')+'<div class="acts">'+b.items.map(i=>`
    <div class="act ${i.severity}"><div class="sev"></div>
      <div><div class="dom">${esc(i.severity)} · ${esc(i.domain)} · <b>${esc(i.agent)}</b></div>
        <div class="ttl">${esc(i.title)}</div><div class="det">${esc(i.detail)}</div>
        <div class="cmd">“${esc(i.command)}”</div></div>
      <button class="btn sm" data-ask="${attr(i.command)}">Hand to agent</button></div>`).join('')+'</div>';
}
function rChecklist(b){
  return (b.title?`<h4 class="blk">${esc(b.title)}</h4>`:'')+'<div class="ck">'+b.items.map(i=>`
    <div class="row ${i.ok?'':'no'}"><div class="ic">${i.ok?'✓':'✕'}</div>
      <div><div>${esc(i.label)}</div><div class="who">${esc(i.agent||'')}</div></div>
      <div class="dt">${esc(i.detail)}</div></div>`).join('')+'</div>';
}
function rBars(b){
  return (b.title?`<h4 class="blk">${esc(b.title)}</h4>`:'')+'<div class="bars">'+b.items.map(i=>{
    const pct=i.max?Math.round(100*i.value/i.max):0, w=Math.min(100,pct);
    const cls=i.value>i.max?'over':(b.unit==='hrs'&&pct<75)?'low':'';
    return `<div class="brow"><div class="bl" title="${attr(i.label)}">${esc(i.label)}</div>
      <div class="bt"><i class="${cls}" style="width:${w}%"></i></div>
      <div class="bv">${i.value}/${i.max} · ${pct}%</div></div>`}).join('')+'</div>';
}
let DOCN=0;
function rDocument(b){
  const id='doc'+(++DOCN), draft=b.status!=='issued';
  const body=esc(b.body).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/\n/g,'<br>');
  return `<div class="doc" id="${id}">${draft?'<div class="wm">DRAFT</div>':''}
    <div class="lh"><div class="seal">VI</div><div class="inst">${esc(b.institute)}
      <small>Autonomous · coastal Karnataka · Office of the Registrar</small></div></div>
    <div class="meta"><span>No. ${esc(b.serial)}</span><span>Date: ${esc(b.date)}</span></div>
    <h3>${esc(b.title)}</h3><div class="bd">${body}</div>
    <div class="sig">${draft?'<em>awaiting approval</em>':'Sd/-'}<br><b>${esc(b.signatory)}</b></div>
    <div class="spec">Specimen generated from VidyaERP's synthetic demo data — not a document of any real institution.</div></div>
    ${draft?'':`<div class="doc-actions"><button class="btn sm ghost" data-print="${id}">Print certificate</button></div>`}`;
}
// The approval moment. A write the agents prepared but did not make is shown
// as waiting for you; one that was approved and made carries the stamp. Both
// are read from the server's own trace, never inferred from wording.
function approvalMark(trace){
  const t=trace||[];
  const i=t.findIndex(s=>/write_authorisation/.test(s.action||''));
  if(i>=0){
    // Only what happened from the authorisation on is about the write (#58): a
    // model that failed over BEFORE it is not a problem with what was written.
    const after=t.slice(i);
    const verified=after.some(s=>s.action==='verify'&&s.status!=='error');
    const failed=after.some(s=>s.status==='error');
    return `<div class="stamp"><b>Approved</b><small>${failed?'written, with a problem reported below':verified?'written and verified':'written'}</small></div>`;
  }
  return '';
}
function rBlock(b){
  if(b.type==='text' && /^\*\*Nothing has been written yet\.\*\*/.test(b.md))
    return `<div class="held-note"><span>${md(b.md.replace(/^\*\*Nothing has been written yet\.\*\*\s*/,
      '**Prepared, and waiting for your approval.** Nothing has been written. '))}</span></div>`;
  if(b.type==='text')   return `<p>${md(b.md)}</p>`;
  if(b.type==='table')  return rTable(b);
  if(b.type==='cards')  return rCards(b);
  if(b.type==='plans')  return rPlans(b);
  if(b.type==='notice') return rNotice(b);
  if(b.type==='grid')   return rGrid(b);
  if(b.type==='actions')   return rActions(b);
  if(b.type==='checklist') return rChecklist(b);
  if(b.type==='bars')      return rBars(b);
  if(b.type==='document')  return rDocument(b);
  return '';
}

/* ---------------- a small dialog, shared by both pages ----------------
   Styled inline so neither page has to carry extra CSS. Resolves to the field
   values, or null when cancelled. */
function dialog(title, fields, note){
  return new Promise(res=>{
    const w=document.createElement('div');
    w.style.cssText='position:fixed;inset:0;z-index:100;background:rgba(15,23,42,.45);display:grid;place-items:center;padding:16px';
    w.innerHTML=`<form style="width:min(400px,100%);background:#fff;border-radius:12px;padding:18px 18px 14px;box-shadow:0 20px 50px rgba(0,0,0,.25);font:13px/1.5 Inter,system-ui,sans-serif;color:#101828">
      <div style="font-size:15px;font-weight:650;margin-bottom:10px">${esc(title)}</div>
      ${fields.map((f,i)=>`<label style="display:block;margin:8px 0 3px;color:#667085;font-size:11px;font-weight:600">${esc(f.label)}</label>
        <input name="f${i}" type="${f.type||'text'}" autocomplete="${f.auto||'off'}" style="width:100%;height:36px;padding:0 10px;border:1px solid #d0d5dd;border-radius:8px;font:inherit"/>`).join('')}
      <div data-note style="min-height:18px;margin-top:8px;color:#667085;font-size:11.5px">${esc(note||'')}</div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:6px">
        <button type="button" data-x style="height:34px;padding:0 14px;border:1px solid #d0d5dd;border-radius:8px;background:#fff;cursor:pointer">Cancel</button>
        <button type="submit" style="height:34px;padding:0 14px;border:0;border-radius:8px;background:#3157d5;color:#fff;font-weight:600;cursor:pointer">OK</button></div></form>`;
    document.body.appendChild(w);
    const form=w.querySelector('form'), done=v=>{w.remove();res(v)};
    w.querySelector('[data-x]').onclick=()=>done(null);
    w.addEventListener('keydown',e=>{if(e.key==='Escape')done(null)});
    form.onsubmit=e=>{e.preventDefault();done(fields.map((_,i)=>form.elements['f'+i].value))};
    setTimeout(()=>form.elements.f0&&form.elements.f0.focus(),20);
  });
}
function notify(msg,ok){
  const t=document.createElement('div');
  t.style.cssText=`position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:101;padding:10px 16px;border-radius:9px;color:#fff;font:13px Inter,system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.25);background:${ok===false?'#c43247':'#101828'}`;
  t.textContent=msg;document.body.appendChild(t);setTimeout(()=>t.remove(),4200);
}
async function changePassword(){
  const v=await dialog('Change your password',[{label:'Current password',type:'password',auto:'current-password'},
    {label:'New password (8+ characters)',type:'password',auto:'new-password'}]);
  if(!v)return;
  const r=await fetch('/api/auth/password',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({current:v[0],new:v[1]})}).then(x=>x.json());
  notify(r.ok?'Password changed.':(r.error||'Could not change it.'),!!r.ok);
}
async function signOut(){await fetch('/api/auth/logout',{method:'POST'});location.href='/login';}
