/* client portal SPA — Cloudflare Pages frontend.
 * Talks to the fly.io backend JSON API (/api/*) with a bearer session
 * token in localStorage. EN default; EN/ZH labels per element. */
const API_BASE = "https://tokencoup-skills-outreach-scheduler.fly.dev";
// ^ set to the fly app URL; when serving the API from another domain,
//   change this one line (see docs/client-portal.md deployment section).

const $app = document.getElementById("app");
const $nav = document.getElementById("nav");
let token = localStorage.getItem("portal_token") || "";
let me = null;

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json",
                    ...(opts.headers || {}) };
  if (token) headers["X-Auth-Token"] = token;
  const res = await fetch(API_BASE + path, { ...opts, headers });
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText),
                                   { status: res.status, data });
  return data;
}

function esc(s) { return String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c])); }

function setToken(t, role, email) {
  token = t || "";
  if (t) {
    localStorage.setItem("portal_token", t);
    localStorage.setItem("portal_role", role || "");
    localStorage.setItem("portal_email", email || "");
  } else {
    localStorage.removeItem("portal_token");
    localStorage.removeItem("portal_role");
    localStorage.removeItem("portal_email");
  }
  me = t ? { role: role || localStorage.getItem("portal_role"),
            email: email || localStorage.getItem("portal_email") } : null;
}

function nav(active) {
  const items = [];
  if (token) {
    items.push(["#/dashboard", "dashboard", "看板"]);
    items.push(["#/reports", "reports", "报告"]);
    items.push(["#/new", "new project", "新建项目"]);
    if (me && me.role === "admin")
      items.push(["#/admin", "admin", "管理"]);
    items.push(["logout", "log out", "退出"]);
  } else {
    items.push(["#/login", "log in", "登录"]);
  }
  $nav.innerHTML = items.map(([href, en, zh]) => href === "logout"
    ? `<button onclick="logout()">${esc(en)}</button>`
    : `<a href="${href}" style="${active === en ? "color:var(--text);font-weight:600" : ""}">${esc(en)}</a>`
  ).join("");
}

async function logout() {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } catch (e) {}
  setToken("", "", "");
  location.hash = "#/login";
}

function card(html) { return `<div class="card">${html}</div>`; }

// ---------------------------------------------------------------- routes
const routes = {
  "#/login": pageAuth, "#/dashboard": pageDashboard,
  "#/reports": pageReports, "#/new": pageNew, "#/admin": pageAdmin,
  "#/oauth": pageOAuth,
};

async function render() {
  const hash = location.hash || "#/login";
  const base = hash.split("?")[0];   // "#/oauth?c=…" routes as "#/oauth"
  const fn = routes[base] || pageAuth;
  try { await fn(hash); } catch (e) {
    if (e.status === 401) { setToken("", "", ""); location.hash = "#/login"; }
    $app.innerHTML = card(`<p class="warn">${esc(e.message)}</p>`);
  }
  nav(base.replace("#/", ""));
}
window.addEventListener("hashchange", render);

// ---------------------------------------------------------------- auth
async function oauthStatus() {
  try { return await api("/api/oauth/status"); }
  catch (e) { return { google: false, facebook: false }; }
}

async function pageAuth() {
  if (token) { location.hash = "#/dashboard"; return; }
  const st = await oauthStatus();
  const gbtn = st.google
    ? `<a class="oauthbtn" href="${API_BASE}/auth/google">Sign in with Google</a>`
    : `<button type="button" disabled title="Google sign-in not configured yet">Sign in with Google</button>`;
  const fbtn = st.facebook
    ? `<a class="oauthbtn" href="${API_BASE}/auth/facebook">Sign in with Facebook</a>`
    : `<button type="button" disabled title="Facebook sign-in not configured yet">Sign in with Facebook</button>`;
  $app.innerHTML = `
  <h1>Client portal</h1>
  <div id="notice"></div>
  ${card(`<h2>Log in</h2>
    <form onsubmit="return doLogin(event)">
      <p><input name="email" placeholder="email" required></p>
      <p><input name="password" type="password" placeholder="password" required></p>
      <p class="row"><button type="submit">Log in</button></p>
      <p class="row">${gbtn} ${fbtn}</p>
    </form>`)}
  ${card(`<h2>Create an account</h2>
    <p class="muted">Registration sends a verification mail; verifying
    grants trial credits when you create your first promotion project.</p>
    <form onsubmit="return doRegister(event)">
      <p><input name="email" placeholder="email" required></p>
      <p><input name="password" type="password"
         placeholder="password (min 8 chars)" required minlength="8"></p>
      <p><button type="submit">Register</button></p>
    </form>`)}`;
}

// OAuth handoff: backend redirects here with a one-time code in the
// hash fragment; exchange it for the session token (token never in a URL).
async function pageOAuth(hash) {
  const m = /c=([^&]+)/.exec(hash || "");
  if (!m) {
    $app.innerHTML = card("<p class='warn'>Missing sign-in code.</p>");
    return;
  }
  try {
    const d = await api("/api/oauth/exchange", { method: "POST",
      body: JSON.stringify({ code: decodeURIComponent(m[1]) }) });
    setToken(d.token, d.role, d.email);
    location.hash = "#/dashboard";
  } catch (e) {
    $app.innerHTML = card(`<p class="warn">Sign-in failed: ${esc(e.message)}</p>
      <p><a href="#/login">Back to login</a></p>`);
  }
}
window.doLogin = async ev => {
  ev.preventDefault();
  const f = ev.target;
  try {
    const d = await api("/api/login", { method: "POST", body: JSON.stringify(
      { email: f.email.value, password: f.password.value }) });
    setToken(d.token, d.role, d.email);
    location.hash = "#/dashboard";
  } catch (e) { notice(e.message, "warn"); }
  return false;
};
window.doRegister = async ev => {
  ev.preventDefault();
  const f = ev.target;
  try {
    const d = await api("/api/register", { method: "POST", body: JSON.stringify(
      { email: f.email.value, password: f.password.value }) });
    notice(d.dev_link
      ? `DEV: verification link (server console): <code>${esc(d.dev_link)}</code>`
      : "Verification mail sent — check your inbox.", "ok");
  } catch (e) { notice(e.message, "warn"); }
  return false;
};
function notice(html, cls) {
  document.getElementById("notice")?.closest("div") ??
    ($app.insertAdjacentHTML("afterbegin", `<div id="notice"></div>`));
  const el = document.getElementById("notice");
  if (el) el.innerHTML = `<p class="${cls}">${html}</p>`;
}

// ---------------------------------------------------------------- dashboard
async function pageDashboard() {
  requireAuth();
  const d = await api("/api/dashboard");
  let html = `<h1>Welcome, ${esc(d.email)}</h1>`;
  if (!d.projects.length) {
    html += card(`<p class="muted">No active project yet.
      <a href="#/new">Create your promotion project</a> — first verified
      project gets 3 trial live links.</p>`);
    $app.innerHTML = html; return;
  }
  html += card(`<h2>Quota consumption</h2>
    <table><tr><th>project</th><th>type</th><th>live / quota</th>
    <th>live (month)</th><th>in flight</th><th>pending</th><th>live (30d)</th></tr>
    ${d.projects.map(p => `<tr><td>${esc(p.domain)}</td><td>${esc(p.quota_type)}</td>
      <td>${p.live} / ${p.quota_total}</td><td>${p.live_month}</td>
      <td>${p.inflight}</td><td>${p.pending}</td><td>${p.live30}</td></tr>`).join("")}
    </table>
    <p class="muted">Only confirmed live links consume credits; in-flight and
    never-live items are not billed.</p>`);
  html += card(`<h2>Delivery capacity today</h2>
    ${d.capacity.length ? `<table><tr><th>placement domain</th><th>addresses used</th></tr>
      ${d.capacity.map(c => `<tr><td>${esc(c.placement)}</td><td>${c.used} / 100</td></tr>`).join("")}
      </table>` : `<p class="muted">nothing executed today</p>`}
    <p class="muted">Per-domain platform budget: 100 addresses / 24h.</p>`);
  html += card(`<h2>Live-link ledger</h2>
    ${d.ledger.length ? `<table><tr><th>project</th><th>placement</th><th>DR</th><th>confirmed</th></tr>
      ${d.ledger.map(l => `<tr><td>${esc(l.project)}</td><td>${esc(l.placement)}</td>
        <td>${l.dr == null ? "—" : Math.round(l.dr)}</td><td>${esc(l.checked_at)}</td></tr>`).join("")}
      </table>` : `<p class="muted">no live links confirmed yet</p>`}
    <p class="muted">Tier 1 evidence — each row is a consumed credit with a receipt.
    <a href="#/reports">Weekly report archive →</a></p>`);
  const b = d.dr_buckets;
  html += card(`<h2>Placement quality</h2>
    <p>DR distribution — &lt;30: <b>${b["<30"]}</b>, 30–60: <b>${b["30-60"]}</b>,
    60+: <b>${b["60+"]}</b>, unranked: <b>${b.unknown}</b></p>
    <table><tr><th>source type</th><th>live links</th></tr>
    ${d.source_mix.map(m => `<tr><td>${esc(m.stype)}</td><td>${m.n}</td></tr>`).join("") ||
      "<tr><td class='muted'>no data yet</td></tr>"}</table>`);
  html += card(`<h2>Funnel trend (weekly)</h2>
    <table><tr><th>week starting</th><th>submitted</th><th>confirmed live</th><th>live rate</th></tr>
    ${d.funnel.map(w => `<tr><td>${esc(w.week)}</td><td>${w.submitted}</td>
      <td>${w.live}</td><td>${w.rate}</td></tr>`).join("")}</table>`);
  $app.innerHTML = html;
}

// ---------------------------------------------------------------- reports
async function pageReports() {
  requireAuth();
  const d = await api("/api/reports");
  let html = "<h1>Report archive (last 7 days)</h1>";
  html += d.reports.length
    ? d.reports.map(r => card(`<h2>${esc(r.domain)}</h2><pre>${esc(r.text)}</pre>`)).join("")
    : card("<p class='muted'>No active project yet.</p>");
  $app.innerHTML = html;
}

// ---------------------------------------------------------------- new project
async function pageNew() {
  requireAuth();
  const d = await api("/api/requests");
  let reqs = "";
  for (const r of d.requests) {
    if (r.verify_state === "draft") {
      reqs += card(`<b>${esc(r.domain)}</b> <span class="muted">— pending
        ownership verification</span>
        <p class="muted">Add this tag inside the <code>&lt;head&gt;</code> of
        your site's homepage:</p>
        <p><code>&lt;meta name='osched-verify' content='${esc(r.verify_token)}'&gt;</code></p>
        <button onclick="verifyDomain(${r.id})">Verify now</button>`);
    } else {
      reqs += card(`<b>${esc(r.domain)}</b>
        <span class="ok">${esc(r.verify_state)}</span>
        <span class="muted">quota ${r.quota_used}/${r.quota_total} (${esc(r.quota_type)})</span>`);
    }
  }
  $app.innerHTML = `<h1>New promotion project</h1>
    ${d.trial_used ? "" : `<p class="ok">First verified project gets 3 trial live links.</p>`}
    <div id="notice"></div>
    ${card(`<form onsubmit="return doNew(event)">
      <p><input name="domain" placeholder="your-domain.com" required></p>
      <p><input name="site_title" placeholder="site title" required></p>
      <p><textarea name="site_summary" rows="3"
         placeholder="one-paragraph site summary"></textarea></p>
      <p><input name="contact_email" placeholder="contact email" required></p>
      <p><input name="tags" placeholder="tags, comma-separated (optional)"></p>
      <p><button type="submit">Create project</button></p>
    </form>`)}
    <h2>Your projects</h2>${reqs || `<p class="muted">none yet</p>`}`;
}
window.doNew = async ev => {
  ev.preventDefault();
  const f = ev.target;
  try {
    const d = await api("/api/new", { method: "POST", body: JSON.stringify({
      domain: f.domain.value, site_title: f.site_title.value,
      site_summary: f.site_summary.value,
      contact_email: f.contact_email.value, tags: f.tags.value }) });
    pageNew();
  } catch (e) { notice(esc(e.message), "warn"); }
  return false;
};
window.verifyDomain = async id => {
  try {
    const d = await api("/api/verify-domain", { method: "POST",
      body: JSON.stringify({ id }) });
    notice(esc(d.message), "ok");
    setTimeout(() => location.hash = "#/dashboard", 1200);
  } catch (e) { notice(esc(e.message), "warn"); }
};

// ---------------------------------------------------------------- admin
async function pageAdmin() {
  requireAuth();
  if (me.role !== "admin") { $app.innerHTML = card("<p class='warn'>Whitelisted admins only.</p>"); return; }
  const d = await api("/api/admin");
  let html = `<h1>Super admin</h1><div id="notice"></div>`;
  html += card(`<h2>Whitelist</h2>
    <p>${d.whitelist.map(w => `<code>${esc(w)}</code>`).join(" ") ||
      "<span class='muted'>empty</span>"}</p>
    <p class="muted">Whitelisted emails get role=admin. Privilege gate,
    not a registration gate.</p>
    <form onsubmit="return doAdmin(event,'whitelist')" class="row">
      <input name="email" placeholder="email to whitelist">
      <button>Add</button></form>`);
  html += card(`<h2>Pending project requests</h2>
    <table><tr><th>client</th><th>domain</th><th>state</th><th></th></tr>
    ${d.drafts.map(r => `<tr><td>${esc(r.email)}</td><td>${esc(r.domain)}</td>
      <td>${esc(r.verify_state)}</td>
      <td><button onclick="doAdminBtn('approve',${r.id})">Approve</button></td></tr>`).join("") ||
      "<tr><td colspan='4' class='muted'>no pending requests</td></tr>"}</table>`);
  html += card(`<h2>Active projects &amp; quotas</h2>
    <table><tr><th>project</th><th>client</th><th>type</th><th>used / total</th><th>set total</th></tr>
    ${d.actives.map(r => `<tr><td>${esc(r.domain)}</td><td>${esc(r.email)}</td>
      <td>${esc(r.quota_type)}</td><td>${r.quota_used} / ${r.quota_total}</td>
      <td><form onsubmit="return doAdmin(event,'quota',${r.id})" class="row">
        <input name="quota_total" value="${r.quota_total}" style="width:70px">
        <button>Set</button></form></td></tr>`).join("") ||
      "<tr><td colspan='5' class='muted'>no active projects</td></tr>"}</table>`);
  html += card(`<h2>Clients</h2>
    <table><tr><th>email</th><th>role</th><th></th></tr>
    ${d.clients.map(c => `<tr><td>${esc(c.email)}</td><td>${esc(c.role)}</td>
      <td><button onclick="doAdminBtn('whitelist',null,'${esc(c.email)}')">
        ${d.whitelist.includes(c.email) ? "Remove" : "Whitelist"}</button></td></tr>`).join("")}</table>`);
  $app.innerHTML = html;
}
window.doAdmin = async (ev, action, id) => {
  ev.preventDefault();
  const body = { action };
  if (id != null) body.id = id;
  if (ev.target.email) body.email = ev.target.email.value;
  if (ev.target.quota_total) body.quota_total = ev.target.quota_total.value;
  try {
    const d = await api("/api/admin", { method: "POST",
      body: JSON.stringify(body) });
    notice(esc(d.message), d.ok ? "ok" : "warn");
    pageAdmin(); nav("admin");
  } catch (e) { notice(esc(e.message), "warn"); }
  return false;
};
window.doAdminBtn = (action, id, email) => {
  const body = { action };
  if (id != null) body.id = id;
  if (email) body.email = email;
  api("/api/admin", { method: "POST", body: JSON.stringify(body) })
    .then(() => { pageAdmin(); nav("admin"); })
    .catch(e => notice(esc(e.message), "warn"));
};

function requireAuth() {
  if (!token) { location.hash = "#/login"; throw new Error("log in first"); }
}

function refreshMe() {
  me = token ? { role: localStorage.getItem("portal_role"),
                 email: localStorage.getItem("portal_email") } : null;
}
refreshMe();
render();
