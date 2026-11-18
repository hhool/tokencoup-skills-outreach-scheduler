#!/usr/bin/env python3
"""Build a self-contained bilingual docs site into docs/site/.

For every EN/ZH doc pair (x.md + x.zh.md) emit one HTML page that embeds
both renderings; the language toggle (default EN) and day/night toggle
(default day) switch client-side with localStorage persistence. No
server, no fetch, works from file://.

Build-time only: uses the `markdown` package (pip install markdown);
nothing in this script ships to the runtime image.
"""
from __future__ import annotations

import html
import pathlib
import re
import sys

import markdown

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
OUT = DOCS / "site"

# (page slug, EN source, ZH source, nav label EN, nav label ZH)
PAGES = [
    ("readme", REPO / "README.md", REPO / "README.zh.md", "Overview", "概览"),
    ("business", DOCS / "business.md", DOCS / "business.zh.md", "Business", "业务层"),
    ("client-intake", DOCS / "client-intake.md", DOCS / "client-intake.zh.md", "Client Intake", "客户接入"),
    ("deployment", DOCS / "deployment.md", DOCS / "deployment.zh.md", "Deployment", "部署"),
    ("operations", DOCS / "operations.md", DOCS / "operations.zh.md", "Operations", "运维"),
    ("design", REPO / "DESIGN.md", REPO / "DESIGN.zh.md", "Design", "设计"),
]

TEMPLATE = """<!DOCTYPE html>
<html lang="__LANG_ATTR__" data-theme="day">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — outreach-scheduler docs</title>
<style>
:root[data-theme="day"] {
  --bg:#f7f7f5; --panel:#ffffff; --text:#1a1a1a; --muted:#6b6b6b;
  --border:#dcdcd8; --accent:#2563eb; --code-bg:#f1f1ee; --link:#2563eb;
}
:root[data-theme="night"] {
  --bg:#121417; --panel:#1c1f24; --text:#e4e6ea; --muted:#9aa0a8;
  --border:#2e333a; --accent:#7aa2f7; --code-bg:#23272e; --link:#7aa2f7;
}
* { box-sizing:border-box; }
body { margin:0; font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
  background:var(--bg); color:var(--text); line-height:1.65; }
header { position:sticky; top:0; z-index:10; background:var(--panel);
  border-bottom:1px solid var(--border); padding:10px 18px;
  display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
header .brand { font-weight:700; letter-spacing:.2px; text-decoration:none; color:var(--text); }
header nav { display:flex; gap:4px; flex-wrap:wrap; flex:1; }
header nav a { padding:4px 10px; border-radius:6px; text-decoration:none;
  color:var(--muted); font-size:14px; }
header nav a:hover { background:var(--code-bg); color:var(--text); }
header nav a.active { background:var(--accent); color:#fff; }
.toggles { display:flex; gap:8px; }
.toggles button { cursor:pointer; border:1px solid var(--border); background:var(--panel);
  color:var(--text); border-radius:6px; padding:4px 10px; font-size:13px; }
.toggles button:hover { background:var(--code-bg); }
main { max-width:880px; margin:0 auto; padding:28px 18px 80px; }
article { background:var(--panel); border:1px solid var(--border); border-radius:10px;
  padding:28px 32px; }
h1,h2,h3,h4 { line-height:1.3; color:var(--text); }
a { color:var(--link); }
code { background:var(--code-bg); padding:2px 5px; border-radius:4px;
  font-size:.92em; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
pre { background:var(--code-bg); padding:14px 16px; border-radius:8px;
  overflow-x:auto; border:1px solid var(--border); }
pre code { background:none; padding:0; }
table { border-collapse:collapse; width:100%; margin:14px 0; font-size:.94em; }
th,td { border:1px solid var(--border); padding:7px 10px; text-align:left; }
th { background:var(--code-bg); }
blockquote { border-left:3px solid var(--accent); margin:14px 0; padding:4px 16px;
  color:var(--muted); }
hr { border:none; border-top:1px solid var(--border); margin:26px 0; }
footer { text-align:center; color:var(--muted); font-size:13px; padding:20px; }
</style>
</head>
<body>
<header>
  <a class="brand" href="index.html">outreach-scheduler</a>
  <nav>__NAV__</nav>
  <div class="toggles">
    <button id="lang" type="button">中文</button>
    <button id="theme" type="button">夜视</button>
  </div>
</header>
<main><article>__BODY__</article></main>
<footer>bilingual docs · built from .md sources</footer>
<script>
(function(){
  var root=document.documentElement;
  var langBtn=document.getElementById('lang'), themeBtn=document.getElementById('theme');
  function setLang(l){
    root.setAttribute('data-lang',l);
    document.querySelectorAll('[data-lang-en]').forEach(function(el){
      el.style.display = (el.getAttribute('data-lang-en')==='1') === (l==='en') ? '' : 'none';
    });
    localStorage.setItem('docs-lang',l);
    langBtn.textContent = l==='en' ? '中文' : 'English';
    document.title = document.querySelector(l==='en'?'[data-lang-en="1"] h1':'[data-lang-en="0"] h1')
      ? (document.querySelector(l==='en'?'[data-lang-en="1"] h1':'[data-lang-en="0"] h1').textContent + ' — outreach-scheduler docs')
      : document.title;
    if(window.location.pathname.endsWith('index.html')){
      document.querySelectorAll('nav a').forEach(function(a){
        a.textContent = l==='en' ? a.getAttribute('data-en') : a.getAttribute('data-zh');
      });
    }
  }
  function setTheme(t){
    root.setAttribute('data-theme',t);
    localStorage.setItem('docs-theme',t);
    themeBtn.textContent = t==='day' ? '夜视' : '白天';
  }
  langBtn.addEventListener('click',function(){
    setLang(root.getAttribute('data-lang')==='en'?'zh':'en');
  });
  themeBtn.addEventListener('click',function(){
    setTheme(root.getAttribute('data-theme')==='day'?'night':'day');
  });
  setTheme(localStorage.getItem('docs-theme')||'day');
  setLang(localStorage.getItem('docs-lang')||'en');
})();
</script>
</body>
</html>
"""

MD_EXT = ["tables", "fenced_code"]


def render_md(text: str) -> str:
    # strip the doc-cross-reference language pointer lines, they are
    # meaningless inside the generated site
    text = re.sub(r"^(英文版|中文版)：\S+\s*$", "", text, flags=re.M)
    return markdown.markdown(text, extensions=MD_EXT)


def slug_title(body: str, fallback: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    if not m:
        return fallback
    inner = re.sub(r"<[^>]+>", "", m.group(1))
    return html.unescape(inner).strip() or fallback


def build_page(slug: str, en_path: pathlib.Path, zh_path: pathlib.Path,
               label_en: str, label_zh: str, all_pages) -> tuple[str, str]:
    en_html = render_md(en_path.read_text(encoding="utf-8"))
    zh_html = render_md(zh_path.read_text(encoding="utf-8"))
    en_title = slug_title(en_html, label_en)
    zh_title = slug_title(zh_html, label_zh)
    body = (
        f'<div data-lang-en="1"><h1 style="display:none">{html.escape(en_title)}</h1>{en_html}</div>\n'
        f'<div data-lang-en="0" style="display:none"><h1 style="display:none">{html.escape(zh_title)}</h1>{zh_html}</div>'
    )
    nav = []
    for pslug, _, _, len_, lzh_ in all_pages:
        href = f"{pslug}.html"
        active = " class=\"active\"" if pslug == slug else ""
        nav.append(
            f'<a href="{href}"{active} data-en="{html.escape(len_)}" '
            f'data-zh="{html.escape(lzh_)}">{html.escape(len_ if slug != "index" else lzh_)}</a>'
        )
    nav_s = "\n  ".join(nav)
    title = en_title
    page = (TEMPLATE
            .replace("__LANG_ATTR__", "en")
            .replace("__TITLE__", html.escape(title))
            .replace("__NAV__", nav_s)
            .replace("__BODY__", body))
    return page, en_title


def build_index(all_pages) -> str:
    rows_en, rows_zh = [], []
    for pslug, _, _, len_, lzh_ in all_pages:
        rows_en.append(f'<li><a href="{pslug}.html">{html.escape(len_)}</a></li>')
        rows_zh.append(f'<li><a href="{pslug}.html">{html.escape(lzh_)}</a></li>')
    def card(rows, heading):
        return (f'<div data-lang-en="{"1" if heading=="Documentation" else "0"}">'
                f'<h2>{heading}</h2><ul>{"".join(rows)}</ul></div>')
    body = (card(rows_en, "Documentation") + "\n" +
            card(rows_zh, "文档目录").replace('data-lang-en="0"', 'data-lang-en="0" style="display:none"'))
    nav = "\n  ".join(
        f'<a href="{pslug}.html" data-en="{html.escape(len_)}" data-zh="{html.escape(lzh_)}">{html.escape(len_)}</a>'
        for pslug, _, _, len_, lzh_ in all_pages)
    page = (TEMPLATE
            .replace("__LANG_ATTR__", "en")
            .replace("__TITLE__", "Docs index")
            .replace("__NAV__", nav)
            .replace("__BODY__", f'<h1 style="display:none">outreach-scheduler docs</h1>{body}'))
    return page


def main() -> int:
    OUT.mkdir(exist_ok=True)
    missing = [str(en) for en, zh, *_ in
               [(p[1], p[2]) + tuple(p[3:]) for p in PAGES] if not en.exists()] + \
              [str(zh) for p in PAGES if not p[2].exists()]
    if missing:
        print("missing sources:", *missing, sep="\n  ")
        return 1
    index = build_index(PAGES)
    (OUT / "index.html").write_text(index, encoding="utf-8")
    print(f"wrote index.html")
    for slug, en_path, zh_path, len_, lzh_ in PAGES:
        page, title = build_page(slug, en_path, zh_path, len_, lzh_, PAGES)
        (OUT / f"{slug}.html").write_text(page, encoding="utf-8")
        print(f"wrote {slug}.html  ({title})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
