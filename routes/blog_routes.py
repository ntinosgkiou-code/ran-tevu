"""
Blog system για SEO + content marketing.

Άρθρα ζουν ως markdown files στο `content/blog/<slug>.md`
με YAML frontmatter:
    ---
    title: "..."
    description: "..."
    date: "2026-05-19"
    author: "Κωνσταντίνος Γκιουλμπαξιώτης"
    cover_emoji: "📞"
    tags: ["voice-ai", "missed-calls"]
    ---

    Body markdown εδώ...

Index page στο /blog. Individual articles στο /blog/<slug>.
"""
from flask import Blueprint, render_template_string, abort
from pathlib import Path
import re
from datetime import datetime

bp = Blueprint("blog", __name__)

BLOG_DIR = Path(__file__).resolve().parent.parent / "content" / "blog"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parses --- YAML --- header και επιστρέφει (metadata, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta_raw = parts[1].strip()
    body = parts[2].strip()
    meta = {}
    for line in meta_raw.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip().strip('"').strip("'")
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
        meta[k.strip()] = v
    return meta, body


def _markdown_to_html(md: str) -> str:
    """Lightweight markdown — no external lib needed for our use."""
    html = md
    # Headers
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.M)
    html = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html, flags=re.M)
    html = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, flags=re.M)
    # Bold + italic
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*([^*]+?)\*", r"<em>\1</em>", html)
    # Links
    html = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', html)
    # Lists
    html = re.sub(r"^- (.+)$", r"<li>\1</li>", html, flags=re.M)
    html = re.sub(r"(<li>.+?</li>\n?)+", lambda m: f"<ul>{m.group(0)}</ul>", html, flags=re.S)
    # Paragraphs
    paragraphs = []
    for chunk in html.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith(("<h", "<ul", "<li", "<p", "<blockquote", "<pre", "<table")):
            paragraphs.append(chunk)
        else:
            paragraphs.append(f"<p>{chunk}</p>")
    return "\n\n".join(paragraphs)


def load_articles() -> list:
    """Returns list of {slug, meta, body, html} sorted by date desc."""
    if not BLOG_DIR.exists():
        return []
    articles = []
    for path in BLOG_DIR.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        meta, body = _parse_frontmatter(text)
        articles.append({
            "slug": path.stem,
            "meta": meta,
            "body": body,
            "html": _markdown_to_html(body),
        })
    articles.sort(key=lambda a: a["meta"].get("date", "0"), reverse=True)
    return articles


BLOG_INDEX_HTML = """<!DOCTYPE html>
<html lang="el"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blog — Ran-Tevu | AI Voice Receptionist Insights</title>
<meta name="description" content="Άρθρα και guides για AI voice automation, missed calls, booking systems στην Ελλάδα.">
<link rel="canonical" href="https://www.ran-tevu.gr/blog">
<style>
  body { font-family: -apple-system,system-ui,sans-serif; background: #050510; color: #E5E7EB; margin: 0; padding: 0; }
  .nav-bar { padding: 16px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); background: rgba(5,5,16,0.8); backdrop-filter: blur(20px); position: sticky; top: 0; z-index: 50; }
  .nav-bar a { color: white; text-decoration: none; font-weight: 700; }
  .container { max-width: 900px; margin: 0 auto; padding: 60px 24px; }
  h1 { font-size: 48px; font-weight: 900; letter-spacing: -2px; margin: 0 0 16px; background: linear-gradient(135deg, #FBBF24, #C084FC); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .lede { color: rgba(255,255,255,0.7); font-size: 18px; margin-bottom: 50px; }
  .article-card {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px;
    padding: 24px;
    margin-bottom: 16px;
    text-decoration: none;
    color: inherit;
    display: block;
    transition: all 0.25s;
  }
  .article-card:hover { border-color: rgba(124,58,237,0.4); transform: translateY(-2px); }
  .article-card .cover { font-size: 36px; margin-bottom: 12px; }
  .article-card h2 { color: white; margin: 0 0 8px; font-size: 22px; }
  .article-card .meta { color: rgba(255,255,255,0.5); font-size: 13px; margin-bottom: 12px; }
  .article-card p { color: rgba(255,255,255,0.7); margin: 0; }
  .empty { text-align: center; color: rgba(255,255,255,0.5); padding: 40px; }
</style>
</head><body>
<div class="nav-bar"><a href="/">← Ran-Tevu</a></div>
<div class="container">
  <h1>Blog</h1>
  <p class="lede">Insights για AI voice automation, missed calls, και πώς οι ελληνικές SMBs σταματούν να χάνουν πελάτες.</p>

  {% if articles %}
    {% for a in articles %}
      <a class="article-card" href="/blog/{{ a.slug }}">
        <div class="cover">{{ a.meta.get('cover_emoji', '📝') }}</div>
        <h2>{{ a.meta.get('title', a.slug) }}</h2>
        <div class="meta">{{ a.meta.get('date', '') }} · {{ a.meta.get('author', '') }}</div>
        <p>{{ a.meta.get('description', '') }}</p>
      </a>
    {% endfor %}
  {% else %}
    <div class="empty">Δεν υπάρχουν άρθρα ακόμα. Έρχονται σύντομα!</div>
  {% endif %}
  <section style="margin:48px 0 8px;padding:30px 26px;border:1px solid #e7e5e4;border-radius:16px;background:#fafaf9;text-align:center">
    <h3 style="font-size:20px;margin:0 0 6px;color:#1c1917">Θες τέτοια κάθε Τρίτη στο inbox σου;</h3>
    <p style="font-size:14.5px;color:#57534e;margin:0 0 16px">Το δωρεάν newsletter για AI στην ιατρική — 4 λεπτά, χωρίς hype, στα ελληνικά.</p>
    <form onsubmit="var e=this.email.value,m=this.querySelector('.nlm');m.textContent='...';fetch('/api/newsletter/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:e})}).then(r=>r.json()).then(d=>{m.textContent=d.ok?'Έγινε! Τα λέμε την Τρίτη.':(d.msg||'Δοκίμασε ξανά.');if(d.ok)this.reset();});return false;" style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center">
      <input name="email" type="email" required placeholder="Το email σου" style="flex:1;min-width:200px;max-width:300px;padding:11px 13px;border:1px solid #d6d3d1;border-radius:9px;font-size:14.5px">
      <button type="submit" style="padding:11px 20px;background:#1c1917;color:#fff;border:none;border-radius:9px;font-weight:600;font-size:14px;cursor:pointer">Εγγραφή</button>
    </form>
    <p class="nlm" style="font-size:12.5px;color:#0d9488;margin:10px 0 0;min-height:16px"></p>
  </section>
</div>
</body></html>"""


BLOG_ARTICLE_HTML = """<!DOCTYPE html>
<html lang="el"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ meta.get('title', slug) }} — Ran-Tevu Blog</title>
<meta name="description" content="{{ meta.get('description', '') }}">
<link rel="canonical" href="https://www.ran-tevu.gr/blog/{{ slug }}">

<meta property="og:type" content="article">
<meta property="og:title" content="{{ meta.get('title', '') }}">
<meta property="og:description" content="{{ meta.get('description', '') }}">
<meta property="og:url" content="https://www.ran-tevu.gr/blog/{{ slug }}">

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "{{ meta.get('title', '') }}",
  "datePublished": "{{ meta.get('date', '') }}",
  "author": { "@type": "Person", "name": "{{ meta.get('author', 'Κωνσταντίνος Γκιουλμπαξιώτης') }}" },
  "publisher": { "@type": "Organization", "name": "Ran-Tevu", "url": "https://www.ran-tevu.gr" }
}
</script>

<style>
  body { font-family: -apple-system,system-ui,sans-serif; background: #050510; color: #E5E7EB; margin: 0; padding: 0; line-height: 1.7; }
  .nav-bar { padding: 16px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); background: rgba(5,5,16,0.8); backdrop-filter: blur(20px); position: sticky; top: 0; z-index: 50; }
  .nav-bar a { color: white; text-decoration: none; font-weight: 700; }
  .article { max-width: 760px; margin: 0 auto; padding: 60px 24px; }
  .article .cover { font-size: 60px; margin-bottom: 20px; text-align: center; }
  .article .meta { color: rgba(255,255,255,0.5); font-size: 13px; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
  .article h1 { font-size: 42px; font-weight: 900; letter-spacing: -2px; margin: 0 0 20px; line-height: 1.15; color: white; }
  .article h2 { font-size: 28px; font-weight: 800; margin: 50px 0 16px; color: #FBBF24; }
  .article h3 { font-size: 22px; font-weight: 700; margin: 36px 0 12px; color: #C084FC; }
  .article p { font-size: 17px; color: rgba(255,255,255,0.85); margin-bottom: 18px; }
  .article ul { padding-left: 24px; margin-bottom: 18px; }
  .article li { margin-bottom: 8px; color: rgba(255,255,255,0.85); }
  .article strong { color: white; }
  .article a { color: #FBBF24; }
  .article a:hover { color: #F59E0B; }
  .cta-end {
    background: linear-gradient(135deg, rgba(124,58,237,0.12), rgba(236,72,153,0.08));
    border: 1px solid rgba(251,191,36,0.4);
    border-radius: 16px;
    padding: 32px;
    margin-top: 60px;
    text-align: center;
  }
  .cta-end h3 { color: white; margin-bottom: 12px; }
  .btn-primary {
    display: inline-block; padding: 14px 28px;
    background: linear-gradient(135deg, #FBBF24, #F59E0B);
    color: #050510; border-radius: 10px; font-weight: 800;
    text-decoration: none; margin-top: 14px;
  }
</style>
</head><body>
<div class="nav-bar"><a href="/blog">← Όλα τα άρθρα</a></div>

<div class="article">
  <div class="cover">{{ meta.get('cover_emoji', '📝') }}</div>
  <div class="meta">{{ meta.get('date', '') }} · {{ meta.get('author', '') }}</div>
  <h1>{{ meta.get('title', slug) }}</h1>

  {{ html | safe }}

  <div class="cta-end">
    <h3>🎙️ Δοκίμασε το Ran-Tevu</h3>
    <p>Live in-browser voice demo. Δες αν η AI πραγματικά μιλάει ελληνικά σαν άνθρωπος.</p>
    <a href="/" class="btn-primary">Μίλα τώρα με την AI →</a>
  </div>
  <section style="margin:36px 0 8px;padding:28px 24px;border:1px solid #e7e5e4;border-radius:16px;background:#fafaf9;text-align:center">
    <h3 style="font-size:19px;margin:0 0 6px;color:#1c1917">Σου άρεσε; Έρχεται ένα τέτοιο κάθε Τρίτη.</h3>
    <p style="font-size:14px;color:#57534e;margin:0 0 15px">Το δωρεάν newsletter για AI στην ιατρική — 4 λεπτά, χωρίς hype, στα ελληνικά.</p>
    <form onsubmit="var e=this.email.value,m=this.querySelector('.nlm');m.textContent='...';fetch('/api/newsletter/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:e})}).then(r=>r.json()).then(d=>{m.textContent=d.ok?'Έγινε! Τα λέμε την Τρίτη.':(d.msg||'Δοκίμασε ξανά.');if(d.ok)this.reset();});return false;" style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center">
      <input name="email" type="email" required placeholder="Το email σου" style="flex:1;min-width:200px;max-width:300px;padding:11px 13px;border:1px solid #d6d3d1;border-radius:9px;font-size:14.5px">
      <button type="submit" style="padding:11px 20px;background:#1c1917;color:#fff;border:none;border-radius:9px;font-weight:600;font-size:14px;cursor:pointer">Εγγραφή</button>
    </form>
    <p class="nlm" style="font-size:12.5px;color:#0d9488;margin:10px 0 0;min-height:16px"></p>
  </section>
</div>
</body></html>"""


@bp.get("/blog")
def blog_index():
    articles = load_articles()
    return render_template_string(BLOG_INDEX_HTML, articles=articles)


@bp.get("/blog/<slug>")
def blog_article(slug: str):
    articles = load_articles()
    article = next((a for a in articles if a["slug"] == slug), None)
    if not article:
        abort(404)
    return render_template_string(
        BLOG_ARTICLE_HTML,
        slug=article["slug"],
        meta=article["meta"],
        html=article["html"],
    )


def register(app):
    app.register_blueprint(bp)
