"""
routes/architecture_routes.py — ΖΩΝΤΑΝΟΣ τεχνικός χάρτης της αρχιτεκτονικής.
================================================================================
Σαρώνει τον ΠΡΑΓΜΑΤΙΚΟ κώδικα (με ast — δεν εκτελεί τίποτα) και υπολογίζει live:
  • το dependency graph (ποιο module κάνει import ποιο)
  • τα «backbone» modules (πόσοι το χρησιμοποιούν = in-degree)
  • δέντρο φακέλων + πλήθος .py
  • DB tables (+ row counts) & routes count
Μένει ΑΚΡΙΒΕΣ καθώς αλλάζει ο κώδικας. Admin-gated.

  GET /admin/architecture                → η σελίδα (canvas graph + πίνακες)
  GET /api/admin/architecture/data?system=rantevu|omnios  → JSON (live scan)
"""
import ast
import os
from collections import defaultdict

from flask import Blueprint, request, jsonify

bp = Blueprint("architecture", __name__)

_RANTEVU_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OMNIOS_ROOT = os.path.join(os.path.dirname(_RANTEVU_ROOT), "AI-Αρχείο", "mit-demo", "core")

_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "content", "VAULT",
              "public", "brain", "docs", "pilot-kit"}


def _gate():
    from admin_auth import admin_ok, token_from_request
    return admin_ok(token_from_request())


def _scan(root: str, include_tests: bool = False) -> dict:
    """Σαρώνει έναν φάκελο, χτίζει το dependency graph μεταξύ ΤΟΠΙΚΩΝ modules (ast, χωρίς εκτέλεση)."""
    if not os.path.isdir(root):
        return {"error": f"δεν βρέθηκε: {root}"}
    # 1) μάζεψε όλα τα local .py → canonical dotted id (relative, folder.name)
    files = []          # (canonical, abspath, folder)
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in _SKIP_DIRS]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            if not include_tests and (fn.startswith("test_") or dp.endswith(os.sep + "tests")):
                continue
            rel = os.path.relpath(os.path.join(dp, fn), root)
            canon = rel[:-3].replace(os.sep, ".")
            folder = rel.split(os.sep)[0] if os.sep in rel else "(root)"
            files.append((canon, os.path.join(dp, fn), folder))
    canon_set = {c for c, _, _ in files}
    by_base = {}
    for c, _, _ in files:
        base = c.split(".")[-1]
        by_base.setdefault(base, []).append(c)
    # 2) parse imports ανά αρχείο
    edges = defaultdict(set)
    loc = {}
    for canon, path, folder in files:
        loc[canon] = folder
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                tree = ast.parse(f.read(), filename=path)
        except Exception:
            continue
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    names.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    names.add(node.module)
        for n in names:
            target = None
            if n in canon_set:
                target = n
            else:
                first, last = n.split(".")[0], n.split(".")[-1]
                for key in (n, first, last):
                    if key in by_base and len(by_base[key]) == 1:
                        target = by_base[key][0]
                        break
            if target and target != canon:
                edges[canon].add(target)
    # 3) in-degree (πόσοι το κάνουν import = πόσο «backbone» είναι)
    indeg = defaultdict(int)
    for src, tgts in edges.items():
        for t in tgts:
            indeg[t] += 1
    nodes = [{"id": c, "folder": loc.get(c, "(root)"),
              "in": indeg.get(c, 0), "out": len(edges.get(c, ()))} for c in canon_set]
    nodes.sort(key=lambda x: (-x["in"] - x["out"], x["id"]))
    edge_list = [[s, t] for s, tgts in edges.items() for t in tgts]
    # folder summary
    fold = defaultdict(int)
    for c in canon_set:
        fold[loc.get(c, "(root)")] += 1
    return {"root": root, "module_count": len(canon_set), "edge_count": len(edge_list),
            "nodes": nodes, "edges": edge_list,
            "folders": sorted(([f, n] for f, n in fold.items()), key=lambda x: -x[1])}


@bp.get("/api/admin/architecture/data")
def architecture_data():
    if not _gate():
        return jsonify({"error": "unauthorized"}), 401
    system = request.args.get("system", "rantevu")
    root = _OMNIOS_ROOT if system == "omnios" else _RANTEVU_ROOT
    out = {"system": system}
    out["graph"] = _scan(root)
    # live extras μόνο για το τρέχον app (rantevu)
    if system == "rantevu":
        try:
            from flask import current_app
            out["routes"] = sum(1 for r in current_app.url_map.iter_rules())
        except Exception:
            out["routes"] = None
        try:
            import database as db
            with db._conn() as c:
                tabs = [r["name"] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            out["tables"] = tabs
        except Exception:
            out["tables"] = []
    return jsonify(out)


@bp.get("/admin/architecture")
def architecture_page():
    return _PAGE


def register(app):
    app.register_blueprint(bp)
    print("[ARCHITECTURE] ζωντανός χάρτης loaded — /admin/architecture")


_PAGE = r"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/><meta name="robots" content="noindex,nofollow"/>
<title>Ζωντανός Τεχνικός Χάρτης</title>
<style>
 :root{--bg:#0d1117;--sf:#161b22;--ink:#d3dae3;--mut:#8b949e;--line:#26303c;--cyan:#39c5cf;--green:#3fb950;--amber:#d29922;--red:#f0616d;--violet:#a371f7;--blue:#589bff;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:18px 14px;max-width:980px;margin:0 auto}
 h1{font-size:22px;margin:0 0 3px}.sub{color:var(--mut);font-size:13px;margin-bottom:14px}
 .tokrow{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
 .tokrow input{flex:1;min-width:180px;padding:9px 11px;background:var(--sf);border:1px solid var(--line);border-radius:8px;color:var(--ink)}
 .btn{padding:9px 14px;border:1px solid var(--line);background:var(--sf);color:var(--ink);border-radius:8px;cursor:pointer;font-size:13px}
 .btn.on{background:var(--cyan);color:#04222a;border-color:var(--cyan);font-weight:700}
 .card{background:var(--sf);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
 h3{font-size:14px;margin:0 0 10px}.mut{color:var(--mut)}
 .stat{display:inline-block;background:#1c232c;border:1px solid var(--line);border-radius:8px;padding:5px 12px;margin:0 7px 7px 0;font-size:13px}
 .stat b{color:var(--cyan);font-size:16px;font-family:var(--mono)}
 canvas{width:100%;height:460px;display:block;background:#0a0e13;border-radius:10px;border:1px solid var(--line);touch-action:none}
 table{width:100%;border-collapse:collapse;font-size:12.5px}
 th{text-align:left;color:var(--mut);font-family:var(--mono);font-size:11px;text-transform:uppercase;padding:7px 9px;border-bottom:1px solid var(--line)}
 td{padding:7px 9px;border-bottom:1px solid #1b232c;font-family:var(--mono)}
 .bar{height:8px;border-radius:4px;background:var(--cyan);display:inline-block;vertical-align:middle}
 .dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px}
 .lg{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin-top:10px}
 .chips span{display:inline-block;background:#1c232c;border:1px solid var(--line);border-radius:7px;padding:4px 10px;margin:0 6px 6px 0;font-size:12px;font-family:var(--mono)}
 .err{background:#2b0f10;border:2px solid var(--red);border-radius:10px;padding:12px 14px;color:#ff8a8a;display:none}
</style></head><body>
<h1>🗺️ Ζωντανός Τεχνικός Χάρτης</h1>
<div class="sub">Σαρώνει τον πραγματικό κώδικα live (ast) και σχεδιάζει το dependency graph — ποιο αρχείο κάνει import ποιο.</div>
<div class="tokrow"><input id="tok" type="password" placeholder="admin token"/><button class="btn" onclick="saveTok()">Σύνδεση</button></div>
<div style="margin-bottom:12px"><button class="btn on" id="b-rantevu" onclick="pick('rantevu')">Ran-Tevu (Flask)</button> <button class="btn" id="b-omnios" onclick="pick('omnios')">OmniOS (FastAPI)</button></div>
<div id="err" class="err"></div>
<div class="card"><div id="stats"><span class="mut">Βάλε token & Σύνδεση…</span></div></div>
<div class="card"><h3>Dependency graph — κάθε τελεία = αρχείο, κάθε γραμμή = import <span class="mut" style="font-weight:400">(μεγαλύτερη τελεία = πιο «κεντρικό»)</span></h3>
 <canvas id="cv"></canvas>
 <div class="lg" id="legend"></div></div>
<div class="card"><h3>Backbone — τα αρχεία που χρησιμοποιούνται από τα περισσότερα</h3><div id="backbone"><div class="mut">—</div></div></div>
<div class="card"><h3>Φάκελοι (πλήθος .py)</h3><div id="folders" class="chips"><span class="mut">—</span></div></div>
<div class="card"><h3>Βάση (live) & Routes</h3><div id="live" class="chips"><span class="mut">—</span></div></div>
<script>
let TOK=localStorage.getItem('rt_arch_tok')||'',SYS='rantevu',DATA=null;
document.getElementById('tok').value=TOK;
function saveTok(){TOK=document.getElementById('tok').value.trim();localStorage.setItem('rt_arch_tok',TOK);load();}
function pick(s){SYS=s;document.getElementById('b-rantevu').className='btn'+(s==='rantevu'?' on':'');document.getElementById('b-omnios').className='btn'+(s==='omnios'?' on':'');load();}
function esc(s){return(s==null?'':''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
const FC={'(root)':'#39c5cf','routes':'#3fb950','voice':'#d29922','core':'#39c5cf','claims_defense':'#a371f7','seo':'#589bff','sales':'#a371f7','scraper':'#8b949e','newsletter':'#589bff'};
function col(f){return FC[f]||'#8b949e';}
async function load(){const e=document.getElementById('err');e.style.display='none';
 if(!TOK){document.getElementById('stats').innerHTML='<span class="mut">Βάλε admin token και Σύνδεση.</span>';return;}
 try{const r=await fetch('/api/admin/architecture/data?system='+SYS,{headers:{'Authorization':'Bearer '+TOK}});
  if(r.status===401){e.textContent='🔴 Λάθος admin token';e.style.display='block';return;}
  const j=await r.json();if(j.graph&&j.graph.error){e.textContent='🔴 '+j.graph.error;e.style.display='block';return;}
  DATA=j;render();}
 catch(ex){e.textContent='🔴 Ο server δεν απάντησε: '+ex.message;e.style.display='block';}}
function render(){const g=DATA.graph;
 document.getElementById('stats').innerHTML='<span class="stat">Αρχεία <b>'+g.module_count+'</b></span><span class="stat">Συνδέσεις import <b>'+g.edge_count+'</b></span>'+(DATA.routes?'<span class="stat">Routes <b>'+DATA.routes+'</b></span>':'')+(DATA.tables?'<span class="stat">Πίνακες βάσης <b>'+DATA.tables.length+'</b></span>':'');
 // backbone table
 const top=g.nodes.slice().sort((a,b)=>b.in-a.in).filter(n=>n.in>0).slice(0,15);
 const mx=top.length?top[0].in:1;
 document.getElementById('backbone').innerHTML='<div style="overflow-x:auto"><table><thead><tr><th>Αρχείο</th><th>Φάκελος</th><th>Το κάνουν import</th></tr></thead><tbody>'+
  top.map(n=>'<tr><td><span class="dot" style="background:'+col(n.folder)+'"></span>'+esc(n.id)+'</td><td class="mut">'+esc(n.folder)+'</td><td><span class="bar" style="width:'+Math.max(6,n.in/mx*120)+'px"></span> '+n.in+'</td></tr>').join('')+'</tbody></table></div>';
 // folders
 document.getElementById('folders').innerHTML=g.folders.map(f=>'<span><span class="dot" style="background:'+col(f[0])+'"></span>'+esc(f[0])+' · '+f[1]+'</span>').join('');
 // live
 document.getElementById('live').innerHTML=DATA.tables?DATA.tables.map(t=>'<span>'+esc(t)+'</span>').join(''):'<span class="mut">(μόνο για Ran-Tevu)</span>';
 // legend
 const folders=[...new Set(g.nodes.map(n=>n.folder))];
 document.getElementById('legend').innerHTML=folders.map(f=>'<span><span class="dot" style="background:'+col(f)+'"></span>'+esc(f)+'</span>').join('');
 drawGraph(g);}
function drawGraph(g){
 const cv=document.getElementById('cv'),dpr=window.devicePixelRatio||1;
 const W=cv.clientWidth,H=460;cv.width=W*dpr;cv.height=H*dpr;const ctx=cv.getContext('2d');ctx.scale(dpr,dpr);
 // top N κόμβοι κατά συνολικό degree
 const N=Math.min(34,g.nodes.length);
 const top=g.nodes.slice(0,N);const idset=new Set(top.map(n=>n.id));
 const idx={};top.forEach((n,i)=>idx[n.id]=i);
 const E=g.edges.filter(e=>idset.has(e[0])&&idset.has(e[1]));
 // init σε κύκλο (deterministic)
 const P=top.map((n,i)=>({x:W/2+Math.cos(i/N*6.283)*Math.min(W,H)*0.36,y:H/2+Math.sin(i/N*6.283)*H*0.36,vx:0,vy:0}));
 for(let it=0;it<260;it++){
  for(let i=0;i<N;i++){for(let j=i+1;j<N;j++){let dx=P[i].x-P[j].x,dy=P[i].y-P[j].y,d2=dx*dx+dy*dy+0.01,f=1600/d2;P[i].vx+=dx*f/Math.sqrt(d2);P[i].vy+=dy*f/Math.sqrt(d2);P[j].vx-=dx*f/Math.sqrt(d2);P[j].vy-=dy*f/Math.sqrt(d2);}}
  E.forEach(e=>{let a=P[idx[e[0]]],b=P[idx[e[1]]],dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)+0.01,f=(d-90)*0.012;a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f;});
  for(let i=0;i<N;i++){P[i].vx+=(W/2-P[i].x)*0.002;P[i].vy+=(H/2-P[i].y)*0.002;P[i].x+=P[i].vx*=0.82;P[i].y+=P[i].vy*=0.82;P[i].x=Math.max(30,Math.min(W-30,P[i].x));P[i].y=Math.max(24,Math.min(H-24,P[i].y));}
 }
 ctx.clearRect(0,0,W,H);
 ctx.strokeStyle='rgba(139,148,158,.22)';ctx.lineWidth=1;
 E.forEach(e=>{let a=P[idx[e[0]]],b=P[idx[e[1]]];ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();});
 top.forEach((n,i)=>{let p=P[i],r=4+Math.min(11,n.in*1.4);
  ctx.beginPath();ctx.arc(p.x,p.y,r,0,6.283);ctx.fillStyle=col(n.folder);ctx.fill();
  if(n.in>=2||i<12){ctx.fillStyle='#c3ccd6';ctx.font='10px ui-monospace,monospace';ctx.textAlign='center';ctx.fillText(n.id.split('.').pop(),p.x,p.y-r-3);}});
}
window.addEventListener('resize',()=>{if(DATA)drawGraph(DATA.graph);});
if(TOK)load();
</script></body></html>"""
