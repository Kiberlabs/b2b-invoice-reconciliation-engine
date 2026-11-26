"""Embedded dark-mode executive dashboard (single self-contained HTML page, no CDN, no build step).

Flow: the page asks for the API key, then calls /v1/ledger, /v1/ledger/verify, /v1/reconcile and /v1/export.csv.
Every server-provided value is written with ``textContent`` (never ``innerHTML``) so ledger data cannot inject script.
"""

from __future__ import annotations

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KiberLabs · Invoice Reconciliation</title>
<style>
:root{--bg:#0b0f17;--card:#121826;--line:#1f2a3d;--txt:#e6edf7;--mut:#8b9ab3;--acc:#3ea6ff;--ok:#2ecc71;--warn:#f5b041;--bad:#ef5350}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:16px 24px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0}h1 span{color:var(--acc)}h2{font-size:14px;margin:0 0 10px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
main{padding:20px 24px;display:grid;gap:16px;max-width:1400px;margin:auto}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi b{display:block;font-size:26px}.kpi small{color:var(--mut)}
.cols{display:grid;grid-template-columns:3fr 2fr;gap:16px}@media(max-width:1000px){.cols{grid-template-columns:1fr}}
input,textarea,button,select{background:#0e1422;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit}
button{cursor:pointer;background:#16314f;border-color:#24507d}button:hover{background:#1b3d63}
textarea{width:100%;min-height:260px;font:12px/1.4 ui-monospace,Consolas,monospace}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--mut);font-weight:600}tbody tr{cursor:pointer}tbody tr:hover{background:#18213a}
.badge{padding:2px 9px;border-radius:99px;font-size:12px;font-weight:700}
.PASS{background:#12351f;color:var(--ok)}.REVIEW{background:#3b2d10;color:var(--warn)}.BLOCKED{background:#3d1717;color:var(--bad)}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;align-items:center}.grow{flex:1;min-width:140px}
#err{color:var(--bad);min-height:18px}.mut{color:var(--mut)}.scroll{max-height:380px;overflow:auto}
ul{margin:6px 0 0;padding-left:18px}
</style></head><body>
<header><h1><span>Kiber</span>Labs · Invoice Reconciliation Engine</h1>
<div class="bar" style="margin:0"><input id="key" type="password" placeholder="X-API-Key" autocomplete="off">
<button id="connect">Connect</button></div></header>
<main>
<div id="err"></div>
<section class="kpis" id="kpis"></section>
<div class="cols">
<section class="card"><h2>Ledger &amp; anomaly drill-down</h2>
<div class="bar"><input id="q" class="grow" placeholder="Search invoice, vendor, verdict, anomaly code…"><button id="search">Search</button><button id="export">Export CSV</button></div>
<div class="scroll"><table><thead><tr><th>#</th><th>Invoice</th><th>Vendor</th><th>Verdict</th><th>Risk</th><th>Anomalies</th></tr></thead><tbody id="rows"></tbody></table></div>
<div id="detail" class="mut" style="margin-top:12px">Select a row to see why it was flagged.</div></section>
<section class="card"><h2>Live sample invoice tester</h2>
<div class="bar"><button data-s="clean">Clean (PASS)</button><button data-s="bad">Tampered (BLOCKED)</button><button data-s="ocr">OCR text (EU format)</button></div>
<textarea id="payload" spellcheck="false"></textarea>
<div class="bar" style="margin-top:8px"><button id="run">Reconcile</button><span id="out" class="mut"></span></div></section>
</div></main>
<script>
const $=id=>document.getElementById(id);
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const uniq=()=>Date.now().toString(36).toUpperCase();
const PO={po_number:"PO-7001",vendor_vat:"ESB12345674",lines:[{sku:"CMP-100",quantity:10,unit_price:"25.00"},{sku:"CMP-200",quantity:4,unit_price:"12.50"}]};
const GRN={po_number:"PO-7001",lines:[{sku:"CMP-100",quantity:10},{sku:"CMP-200",quantity:4}]};
const SAMPLES={
 clean:()=>({invoice:{invoice_number:"INV-"+uniq(),vendor:{name:"Northwind Components S.L.",vat:"ESB12345674",iban:"ES9121000418450200051332"},issue_date:"2026-11-20",currency:"EUR",po_number:"PO-7001",lines:[{sku:"CMP-100",description:"Controller board",quantity:10,unit_price:"25.00"},{sku:"CMP-200",description:"Sensor kit",quantity:4,unit_price:"12.50"}],subtotal:"300.00",tax:"63.00",total:"363.00"},po:PO,grn:GRN}),
 bad:()=>({invoice:{invoice_number:"INV-"+uniq(),vendor:{name:"Northwind Components S.L.",vat:"ESB12345674",iban:"ES9121000418450200051333"},issue_date:"2026-11-20",currency:"EUR",po_number:"PO-7001",lines:[{sku:"CMP-100",description:"Controller board",quantity:10,unit_price:"28.00"},{sku:"CMP-200",description:"Sensor kit",quantity:4,unit_price:"12.50"}],subtotal:"330.00",tax:"63.00",total:"400.00"},po:PO,grn:GRN}),
 ocr:()=>({invoice:["INVOICE No: OCR-"+uniq(),"Vendor: Northwind Components S.L.","VAT: ESB12345674","IBAN: ES91 2100 0418 4502 0005 1332","Date: 20/11/2026","PO: PO-7001","CMP-100  Controller board  10 x 25,00","CMP-200  Sensor kit  4 x 12,50","Subtotal: 3OO,OO EUR","VAT 21%: 63,00 EUR","Total: 363,00 EUR"].join("\n"),format:"text",po:PO,grn:GRN})
};
function load(n){$("payload").value=JSON.stringify(SAMPLES[n](),null,2)}
document.querySelectorAll("[data-s]").forEach(b=>b.onclick=()=>load(b.dataset.s));
async function api(path,opts={}){
 const r=await fetch(path,{...opts,headers:{"X-API-Key":$("key").value.trim(),"Content-Type":"application/json"}});
 if(!r.ok){let m=String(r.status);try{const d=(await r.json()).detail;m+=" "+(typeof d==="string"?d:JSON.stringify(d))}catch(e){}throw new Error(m)}
 return r}
let rows=[];
function kpi(label,val,cls){const c=el("div","card kpi");const b=el("b",cls,val);c.append(b,el("small","",label));return c}
function renderKpis(v){
 const n=rows.length,cnt=x=>rows.filter(r=>r.verdict===x).length;
 const avg=n?Math.round(rows.reduce((s,r)=>s+r.risk_score,0)/n):0;
 const k=$("kpis");k.replaceChildren(kpi("Invoices processed",n),kpi("PASS",cnt("PASS"),"PASS badge"),kpi("REVIEW",cnt("REVIEW"),"REVIEW badge"),kpi("BLOCKED",cnt("BLOCKED"),"BLOCKED badge"),kpi("Avg risk score",avg+"/100"),kpi(v.ok?"Ledger chain intact":"Ledger TAMPERED",v.ok?"OK":"BROKEN #"+v.broken_at,v.ok?"PASS badge":"BLOCKED badge"))}
function renderRows(){
 const tb=$("rows");tb.replaceChildren();
 for(const r of rows){const tr=el("tr");
  tr.append(el("td","",r.seq),el("td","",r.invoice_number),el("td","",r.vendor_name));
  const v=el("td");v.append(el("span","badge "+r.verdict,r.verdict));tr.append(v,el("td","",r.risk_score),el("td","mut",r.anomalies.map(a=>a.code).join(", ")||"-"));
  tr.onclick=()=>detail(r);tb.append(tr)}}
function detail(r){
 const d=$("detail");d.className="";d.replaceChildren(el("b","",r.invoice_number+" · "+r.currency+" "+r.total+" · risk "+r.risk_score));
 const ul=el("ul");for(const a of r.anomalies)ul.append(el("li","",a.code+" (+"+a.points+"): "+a.message));
 if(!r.anomalies.length)ul.append(el("li","","No anomalies: clean 3-way match."));
 d.append(ul,el("div","mut","entry_hash "+r.entry_hash.slice(0,24)+"… ← prev "+r.prev_hash.slice(0,12)+"…"))}
async function refresh(){
 $("err").textContent="";
 try{
  const q=$("q").value.trim();
  const [l,v]=await Promise.all([api("/v1/ledger?limit=500"+(q?"&q="+encodeURIComponent(q):"")).then(r=>r.json()),api("/v1/ledger/verify").then(r=>r.json())]);
  rows=l;renderRows();renderKpis(v)}
 catch(e){$("err").textContent="Error: "+e.message}}
$("connect").onclick=refresh;$("search").onclick=refresh;
$("export").onclick=async()=>{try{const r=await api("/v1/export.csv");const u=URL.createObjectURL(await r.blob());
 const a=el("a");a.href=u;a.download="invoice_audit_export.csv";a.click();URL.revokeObjectURL(u)}catch(e){$("err").textContent="Error: "+e.message}};
$("run").onclick=async()=>{
 $("out").textContent="…";
 try{const r=await api("/v1/reconcile",{method:"POST",body:$("payload").value});const d=await r.json();
  $("out").textContent=d.result.verdict+" · risk "+d.result.risk_score+" · "+d.result.anomalies.length+" anomalies";await refresh()}
 catch(e){$("out").textContent="";$("err").textContent="Error: "+e.message}};
load("clean");
</script></body></html>
"""
