/* client portal SPA — Cloudflare Pages frontend.
 * Talks to the fly.io backend JSON API (/api/*) with a bearer session
 * token in localStorage.
 * i18n: EN/ZH dictionaries below; language is persisted in
 * localStorage (portal_lang) and defaults to the browser language.
 * Static marketing pages use separate URLs (/ and /zh/) instead. */
const API_BASE = "https://tokencoup-skills-outreach-scheduler.fly.dev";
// ^ set to the fly app URL; when serving the API from another domain,
//   change this one line (see docs/client-portal.md deployment section).

const $app = document.getElementById("app");
const $nav = document.getElementById("nav");
let token = localStorage.getItem("portal_token") || "";
let me = null;

// ---------------------------------------------------------------- i18n
const DICT = {
  en: {
    "nav.dashboard": "dashboard", "nav.reports": "reports",
    "nav.new": "new project", "nav.pricing": "pricing",
    "nav.admin": "admin", "nav.logout": "log out", "nav.login": "log in",
    "nav.switch": "中文",

    "auth.title": "Client portal",
    "auth.login": "Log in",
    "auth.email": "email", "auth.password": "password",
    "auth.signGoogle": "Sign in with Google",
    "auth.signFacebook": "Sign in with Facebook",
    "auth.googleOff": "Google sign-in not configured yet",
    "auth.facebookOff": "Facebook sign-in not configured yet",
    "auth.create": "Create an account",
    "auth.createDesc": "Registration sends a verification mail; verifying grants trial credits when you create your first promotion project.",
    "auth.passwordMin": "password (min 8 chars)",
    "auth.register": "Register",
    "auth.mailSent": "Verification mail sent — check your inbox.",
    "auth.devLink": "DEV: verification link (server console):",
    "auth.missingCode": "Missing sign-in code.",
    "auth.failed": "Sign-in failed:",
    "auth.backLogin": "Back to login",

    "pricing.title": "Pricing — per verified live link",
    "pricing.sub": "No subscription. You pay for links we confirm <b>live</b> in the ledger; the attestation is the invoice.",
    "pricing.trial": "Trial", "pricing.starter": "Starter",
    "pricing.growth": "Growth", "pricing.scale": "Scale",
    "pricing.free": "$0",
    "pricing.trialLine": "<b>3 live links</b> · 14-day window",
    "pricing.trialNote": "One trial per verified domain — verify ownership and create your first project to claim it.",
    "pricing.claim": "Claim trial →", "pricing.signup": "Sign up →",
    "pricing.perLink": "per live link",
    "pricing.starterVol": "1–9 links/month",
    "pricing.starterNote": "For single-site owners testing directory outreach as a channel.",
    "pricing.growthVol": "10–49 links/month",
    "pricing.growthNote": "Most popular — steady monthly link velocity with full ledger evidence.",
    "pricing.scaleVol": "50+ links/month",
    "pricing.scaleNote": "Agency / portfolio volume; dedicated quota planning.",
    "pricing.request": "Request this package",
    "pricing.createProject": "Create a project",
    "pricing.startTrial": "Start free trial",
    "pricing.guarantee": "Service guarantee",
    "pricing.guaranteeBody": "A link that never reaches <b>live</b> after auto-retry + human fallback is <b>not billed</b>. Non-live states (missing / unreachable / bot-blocked) are work-in-progress — never charged.",
    "pricing.receipt": "Every consumed credit has a ledger row with the confirmed URL and check timestamp — exportable as an invoice attachment.",

    "dash.welcome": "Welcome,",
    "dash.nextSteps": "Next steps",
    "dash.thProject": "project", "dash.thState": "state",
    "dash.thAction": "action",
    "dash.verifyPending": "verify pending",
    "dash.verifyNow": "Verify ownership now",
    "dash.verifyHow": "meta tag / DNS TXT",
    "dash.awaiting": "awaiting approval",
    "dash.reviewNote": "admin reviews within 24h — no action needed",
    "dash.rejected": "rejected",
    "dash.rejectedNote": "contact support or re-create with corrected details",
    "dash.noProject": "No project yet — create your first promotion project to claim the 3-link free trial.",
    "dash.exhausted": "Trial consumed — keep the momentum",
    "dash.exhaustedBody": "Your trial on",
    "dash.exhaustedBody2": "is used up. Choose a package to keep building — you only ever pay for links confirmed live, and the trial ledger stays as evidence.",
    "dash.withheld": "Free trial not granted",
    "dash.withheldBody": "We grant the 3-link trial once per mailbox, once per site, and once per network. These projects are active but start at 0 credits:",
    "dash.withheldTag": "withheld",
    "dash.withheldCta": "Paid links have no such limit — pick a package and we start the next working day.",
    "trial.deny.email": "this e-mail (aliases included) already used the trial",
    "trial.deny.site": "this site (sub-domains included) already used the trial",
    "trial.deny.claimed": "this site is active under another account",
    "trial.deny.ip": "this network already used the trial",
    "trial.deny.used": "this account already used the trial",
    "dash.choosePackage": "Choose a package →",
    "dash.noActive": "No active project yet.",
    "dash.createProject": "Create your promotion project",
    "dash.createProject2": "— first verified project gets 3 trial live links.",
    "dash.quota": "Quota consumption",
    "dash.thType": "type", "dash.thConsumed": "consumed",
    "dash.thCredits": "credits", "dash.thTrial": "trial",
    "dash.thLiveQuota": "live / quota", "dash.thLiveMonth": "live (month)",
    "dash.thInflight": "in flight", "dash.thPending": "pending",
    "dash.thLive30": "live (30d)",
    "dash.used": "used", "dash.left": "left",
    "dash.daysLeft": "d left", "dash.windowClosed": "window closed",
    "dash.quotaNote": "Only confirmed live links consume credits; in-flight and never-live items are not billed.",
    "dash.capacity": "Delivery capacity today",
    "dash.thPlacement": "placement domain",
    "dash.thAddresses": "addresses used",
    "dash.nothingToday": "nothing executed today",
    "dash.budgetNote": "Per-domain platform budget: 100 addresses / 24h.",
    "dash.ledger": "Live-link ledger",
    "dash.thConfirmed": "confirmed",
    "dash.noLive": "no live links confirmed yet",
    "dash.evidence": "Tier 1 evidence — each row is a consumed credit with a receipt.",
    "dash.weekly": "Weekly report archive →",
    "dash.quality": "Placement quality",
    "dash.drDist": "DR distribution — &lt;30:",
    "dash.unranked": "unranked:",
    "dash.thSource": "source type", "dash.thLiveLinks": "live links",
    "dash.noData": "no data yet",
    "dash.funnel": "Funnel trend (weekly)",
    "dash.thWeek": "week starting", "dash.thSubmitted": "submitted",
    "dash.thConfirmedLive": "confirmed live", "dash.thRate": "live rate",

    "rep.title": "Report archive (last 7 days)",
    "rep.none": "No active project yet.",

    "new.title": "New promotion project",
    "new.trialOk": "First verified project gets 3 trial live links.",
    "new.domain": "your-domain.com", "new.siteTitle": "site title",
    "new.summary": "one-paragraph site summary",
    "new.contact": "contact email",
    "new.tags": "tags, comma-separated (optional)",
    "new.create": "Create project",
    "new.yours": "Your projects",
    "new.none": "none yet",
    "new.pendingVerify": "— pending ownership verification",
    "new.tagHint": "Add this tag inside the <code>&lt;head&gt;</code> of your site's homepage:",
    "new.verifyNow": "Verify now",

    "adm.title": "Super admin",
    "adm.whitelist": "Whitelist",
    "adm.empty": "empty",
    "adm.whitelistNote": "Whitelisted emails get role=admin. Privilege gate, not a registration gate.",
    "adm.addEmail": "email to whitelist", "adm.add": "Add",
    "adm.pending": "Pending project requests",
    "adm.thClient": "client", "adm.thDomain": "domain", "adm.thState": "state",
    "adm.approve": "Approve",
    "adm.noPending": "no pending requests",
    "adm.actives": "Active projects &amp; quotas",
    "adm.thProject": "project", "adm.thType": "type",
    "adm.thUsedTotal": "used / total", "adm.thSetTotal": "set total",
    "adm.set": "Set",
    "adm.noActive": "no active projects",
    "adm.packages": "Package requests",
    "adm.thTier": "tier", "adm.thPlanned": "planned",
    "adm.thPrice": "$ / link",
    "adm.done": "Done", "adm.cancel": "Cancel",
    "adm.noPackages": "no pending package requests",
    "adm.packageNote": "Confirm payment offline, then set the project's quota_type=paid via the quota tool above — the ledger keeps serving as the invoice evidence.",
    "adm.clients": "Clients",
    "adm.thEmail": "email", "adm.thRole": "role",
    "adm.remove": "Remove", "adm.whitelistBtn": "Whitelist",
    "adm.adminsOnly": "Whitelisted admins only.",

    "err.loginFirst": "log in first",
    "shell.tag": "client portal",
    "shell.foot": "every link attested live",
  },
  zh: {
    "nav.dashboard": "看板", "nav.reports": "报告",
    "nav.new": "新建项目", "nav.pricing": "定价",
    "nav.admin": "管理", "nav.logout": "退出", "nav.login": "登录",
    "nav.switch": "EN",

    "auth.title": "客户门户",
    "auth.login": "登录",
    "auth.email": "邮箱", "auth.password": "密码",
    "auth.signGoogle": "使用 Google 登录",
    "auth.signFacebook": "使用 Facebook 登录",
    "auth.googleOff": "Google 登录尚未配置",
    "auth.facebookOff": "Facebook 登录尚未配置",
    "auth.create": "创建账号",
    "auth.createDesc": "注册后会发送验证邮件；完成验证并创建第一个推广项目即可获得试用额度。",
    "auth.passwordMin": "密码（至少 8 位）",
    "auth.register": "注册",
    "auth.mailSent": "验证邮件已发送——请查收。",
    "auth.devLink": "开发模式：验证链接（服务器控制台）：",
    "auth.missingCode": "缺少登录凭据。",
    "auth.failed": "登录失败：",
    "auth.backLogin": "返回登录",

    "pricing.title": "定价 —— 按验证上线的实链计费",
    "pricing.sub": "不订阅。只为台账里确认<b>上线</b>的链接付费；验真记录就是发票。",
    "pricing.trial": "试用", "pricing.starter": "入门",
    "pricing.growth": "成长", "pricing.scale": "规模",
    "pricing.free": "$0",
    "pricing.trialLine": "<b>3 条实链</b> · 14 天窗口",
    "pricing.trialNote": "每个已验证域名限一次——验证所有权并创建第一个项目即可领取。",
    "pricing.claim": "领取试用 →", "pricing.signup": "注册 →",
    "pricing.perLink": "每条实链",
    "pricing.starterVol": "1–9 条 / 月",
    "pricing.starterNote": "适合单站站长把目录外链当作一条渠道来试。",
    "pricing.growthVol": "10–49 条 / 月",
    "pricing.growthNote": "最多人选——稳定的月度上链速度，附完整台账证据。",
    "pricing.scaleVol": "50+ 条 / 月",
    "pricing.scaleNote": "代理商 / 多站组合用量；专属额度规划。",
    "pricing.request": "申请该套餐",
    "pricing.createProject": "创建项目",
    "pricing.startTrial": "开始免费试用",
    "pricing.guarantee": "服务保障",
    "pricing.guaranteeBody": "经过自动重试与人工兜底仍未达到 <b>上线</b> 的链接<b>不计费</b>。非上线状态（缺失 / 打不开 / 被拦截）属进行中，永不收费。",
    "pricing.receipt": "每个消耗的额度都有一条台账记录，含确认 URL 与核验时间戳——可直接作为发票附件导出。",

    "dash.welcome": "欢迎，",
    "dash.nextSteps": "下一步",
    "dash.thProject": "项目", "dash.thState": "状态",
    "dash.thAction": "操作",
    "dash.verifyPending": "待验证",
    "dash.verifyNow": "立即验证所有权",
    "dash.verifyHow": "meta 标签 / DNS TXT",
    "dash.awaiting": "待审核",
    "dash.reviewNote": "管理员 24 小时内审核——无需操作",
    "dash.rejected": "已驳回",
    "dash.rejectedNote": "请联系支持，或用更正后的资料重建",
    "dash.noProject": "还没有项目——创建第一个推广项目即可领取 3 条实链试用。",
    "dash.exhausted": "试用额度已用完——趁热继续",
    "dash.exhaustedBody": "你在",
    "dash.exhaustedBody2": "的试用额度已用尽。选一个套餐继续推进——只为确认上线的链接付费，试用期台账仍作为证据保留。",
    "dash.withheld": "免费额度未发放",
    "dash.withheldBody": "3 条免费额度按「邮箱／站点／网络」各限一次。以下项目已激活但额度为 0：",
    "dash.withheldTag": "未发放",
    "dash.withheldCta": "付费链接没有这个限制——选一个套餐，次个工作日开工。",
    "trial.deny.email": "该邮箱（含别名）已用过试用额度",
    "trial.deny.site": "该站点（含子域）已用过试用额度",
    "trial.deny.claimed": "该站点已被其他账号激活",
    "trial.deny.ip": "该网络已用过试用额度",
    "trial.deny.used": "该账号已用过试用额度",
    "dash.choosePackage": "选择套餐 →",
    "dash.noActive": "还没有生效中的项目。",
    "dash.createProject": "创建推广项目",
    "dash.createProject2": "——首个通过验证的项目送 3 条实链试用。",
    "dash.quota": "额度消耗",
    "dash.thType": "类型", "dash.thConsumed": "已消耗",
    "dash.thCredits": "额度", "dash.thTrial": "试用",
    "dash.thLiveQuota": "上线 / 额度", "dash.thLiveMonth": "本月上线",
    "dash.thInflight": "进行中", "dash.thPending": "待处理",
    "dash.thLive30": "近 30 天上线",
    "dash.used": "已用", "dash.left": "剩余",
    "dash.daysLeft": "天剩余", "dash.windowClosed": "窗口已关闭",
    "dash.quotaNote": "只有确认上线的链接消耗额度；进行中与始终未上线的都不计费。",
    "dash.capacity": "今日交付产能",
    "dash.thPlacement": "投放域名",
    "dash.thAddresses": "已用地址数",
    "dash.nothingToday": "今日没有执行记录",
    "dash.budgetNote": "每域名平台预算：100 个地址 / 24 小时。",
    "dash.ledger": "实链台账",
    "dash.thConfirmed": "确认时间",
    "dash.noLive": "还没有确认上线的链接",
    "dash.evidence": "一级证据——每一行都是一个带凭证的已消耗额度。",
    "dash.weekly": "周报存档 →",
    "dash.quality": "投放置量",
    "dash.drDist": "DR 分布 —— &lt;30：",
    "dash.unranked": "未评级：",
    "dash.thSource": "来源类型", "dash.thLiveLinks": "实链数",
    "dash.noData": "暂无数据",
    "dash.funnel": "漏斗趋势（按周）",
    "dash.thWeek": "起始周", "dash.thSubmitted": "已提交",
    "dash.thConfirmedLive": "确认上线", "dash.thRate": "上线率",

    "rep.title": "报告存档（近 7 天）",
    "rep.none": "还没有生效中的项目。",

    "new.title": "新建推广项目",
    "new.trialOk": "首个通过验证的项目送 3 条实链试用。",
    "new.domain": "你的域名.com", "new.siteTitle": "站点标题",
    "new.summary": "一段站点简介",
    "new.contact": "联系邮箱",
    "new.tags": "标签，逗号分隔（可选）",
    "new.create": "创建项目",
    "new.yours": "你的项目",
    "new.none": "还没有",
    "new.pendingVerify": "—— 待验证所有权",
    "new.tagHint": "把这段标签加到站点首页的 <code>&lt;head&gt;</code> 里：",
    "new.verifyNow": "立即验证",

    "adm.title": "超级管理员",
    "adm.whitelist": "白名单",
    "adm.empty": "空",
    "adm.whitelistNote": "白名单邮箱获得 role=admin 权限。这是权限闸，不是注册闸。",
    "adm.addEmail": "要加入白名单的邮箱", "adm.add": "添加",
    "adm.pending": "待审核的项目申请",
    "adm.thClient": "客户", "adm.thDomain": "域名", "adm.thState": "状态",
    "adm.approve": "通过",
    "adm.noPending": "没有待审核申请",
    "adm.actives": "生效项目与额度",
    "adm.thProject": "项目", "adm.thType": "类型",
    "adm.thUsedTotal": "已用 / 总量", "adm.thSetTotal": "设置总量",
    "adm.set": "设置",
    "adm.noActive": "没有生效项目",
    "adm.packages": "套餐申请",
    "adm.thTier": "档位", "adm.thPlanned": "计划条数",
    "adm.thPrice": "$ / 条",
    "adm.done": "完成", "adm.cancel": "取消",
    "adm.noPackages": "没有待处理套餐申请",
    "adm.packageNote": "线下确认收款后，用上面的额度工具把项目 quota_type 设为 paid——台账继续作为发票证据。",
    "adm.clients": "客户",
    "adm.thEmail": "邮箱", "adm.thRole": "角色",
    "adm.remove": "移除", "adm.whitelistBtn": "加白名单",
    "adm.adminsOnly": "仅限白名单管理员。",

    "err.loginFirst": "请先登录",
    "shell.tag": "客户门户",
    "shell.foot": "每条外链都验真上线",
  },
};

let lang = localStorage.getItem("portal_lang") ||
  ((navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en");

function t(key) {
  return (DICT[lang] && DICT[lang][key]) || DICT.en[key] || key;
}
function toggleLang() {
  lang = lang === "zh" ? "en" : "zh";
  localStorage.setItem("portal_lang", lang);
  document.documentElement.lang = lang === "zh" ? "zh-Hans" : "en";
  render();
}
window.toggleLang = toggleLang;

// ---------------------------------------------------------------- api
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

function setToken(tk, role, email) {
  token = tk || "";
  if (tk) {
    localStorage.setItem("portal_token", tk);
    localStorage.setItem("portal_role", role || "");
    localStorage.setItem("portal_email", email || "");
  } else {
    localStorage.removeItem("portal_token");
    localStorage.removeItem("portal_role");
    localStorage.removeItem("portal_email");
  }
  me = tk ? { role: role || localStorage.getItem("portal_role"),
              email: email || localStorage.getItem("portal_email") } : null;
}

function nav(active) {
  const pt = document.getElementById("portaltag");
  const ft = document.getElementById("foottag");
  if (pt) pt.textContent = t("shell.tag");
  if (ft) ft.textContent = t("shell.foot");
  const items = [];
  if (token) {
    items.push(["#/dashboard", "dashboard"]);
    items.push(["#/reports", "reports"]);
    items.push(["#/new", "new"]);
    items.push(["#/pricing", "pricing"]);
    if (me && me.role === "admin") items.push(["#/admin", "admin"]);
    items.push(["logout", "logout"]);
  } else {
    items.push(["#/pricing", "pricing"]);
    items.push(["#/login", "login"]);
  }
  const links = items.map(([href, key]) => href === "logout"
    ? `<button onclick="logout()">${esc(t("nav." + key))}</button>`
    : `<a href="${href}" style="${active === key ? "color:var(--text);font-weight:600" : ""}">${esc(t("nav." + key))}</a>`
  ).join("");
  $nav.innerHTML = links +
    `<button class="langbtn" onclick="toggleLang()" title="switch language">${esc(t("nav.switch"))}</button>`;
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
  "#/oauth": pageOAuth, "#/pricing": pagePricing,
};

async function render() {
  const hash = location.hash || "#/login";
  const base = hash.split("?")[0];   // "#/oauth?c=…" routes as "#/oauth"
  const fn = routes[base] || pageAuth;
  try { await fn(hash); } catch (e) {
    if (e.status === 401) { setToken("", "", ""); location.hash = "#/login"; }
    $app.innerHTML = card(`<p class="warn">${esc(e.message)}</p>`);
  }
  nav((base.replace("#/", "") === "login" ? "" : base.replace("#/", "")));
}
window.addEventListener("hashchange", render);

// ---------------------------------------------------------------- pricing
async function pagePricing() {
  const cta = token
    ? `<a class="btn" href="#/new">${esc(t("pricing.createProject"))}</a>`
    : `<a class="btn" href="#/login">${esc(t("pricing.startTrial"))}</a>`;
  const req = key => token
    ? `<button onclick="requestPackage('${key}')">${esc(t("pricing.request"))}</button>` : "";
  $app.innerHTML = `
  <h1>${esc(t("pricing.title"))}</h1>
  <p class="muted">${t("pricing.sub")}</p>
  <div class="tiers">
    ${card(`<h2>${esc(t("pricing.trial"))}</h2><p class="price">${esc(t("pricing.free"))}</p>
      <p>${t("pricing.trialLine")}</p>
      <p class="muted">${esc(t("pricing.trialNote"))}</p>
      ${token ? `<a href="#/new">${esc(t("pricing.claim"))}</a>` : `<a href="#/login">${esc(t("pricing.signup"))}</a>`}`)}
    ${card(`<h2>${esc(t("pricing.starter"))}</h2><p class="price">$80</p>
      <p>${esc(t("pricing.perLink"))} · ${esc(t("pricing.starterVol"))}</p>
      <p class="muted">${esc(t("pricing.starterNote"))}</p>${req("starter")}`)}
    ${card(`<h2>${esc(t("pricing.growth"))}</h2><p class="price">$65</p>
      <p>${esc(t("pricing.perLink"))} · ${esc(t("pricing.growthVol"))}</p>
      <p class="muted">${esc(t("pricing.growthNote"))}</p>${req("growth")}`)}
    ${card(`<h2>${esc(t("pricing.scale"))}</h2><p class="price">$50</p>
      <p>${esc(t("pricing.perLink"))} · ${esc(t("pricing.scaleVol"))}</p>
      <p class="muted">${esc(t("pricing.scaleNote"))}</p>${req("scale")}`)}
  </div>
  ${card(`<h2>${esc(t("pricing.guarantee"))}</h2>
    <p>${t("pricing.guaranteeBody")}</p>
    <p class="muted">${esc(t("pricing.receipt"))}</p>`)}
  <div class="center">${cta}</div>
  <div id="notice"></div>`;
}
window.requestPackage = async tier => {
  try {
    const d = await api("/api/request-package", { method: "POST",
      body: JSON.stringify({ tier }) });
    notice(esc(d.message), "ok");
  } catch (e) { notice(esc(e.message), "warn"); }
};

// ---------------------------------------------------------------- auth
async function oauthStatus() {
  try { return await api("/api/oauth/status"); }
  catch (e) { return { google: false, facebook: false }; }
}

async function pageAuth() {
  if (token) { location.hash = "#/dashboard"; return; }
  const st = await oauthStatus();
  const gbtn = st.google
    ? `<a class="oauthbtn" href="${API_BASE}/auth/google">${esc(t("auth.signGoogle"))}</a>`
    : `<button type="button" disabled title="${esc(t("auth.googleOff"))}">${esc(t("auth.signGoogle"))}</button>`;
  const fbtn = st.facebook
    ? `<a class="oauthbtn" href="${API_BASE}/auth/facebook">${esc(t("auth.signFacebook"))}</a>`
    : `<button type="button" disabled title="${esc(t("auth.facebookOff"))}">${esc(t("auth.signFacebook"))}</button>`;
  $app.innerHTML = `
  <h1>${esc(t("auth.title"))}</h1>
  <div id="notice"></div>
  ${card(`<h2>${esc(t("auth.login"))}</h2>
    <form onsubmit="return doLogin(event)">
      <p><input name="email" placeholder="${esc(t("auth.email"))}" required></p>
      <p><input name="password" type="password" placeholder="${esc(t("auth.password"))}" required></p>
      <p class="row"><button type="submit">${esc(t("auth.login"))}</button></p>
      <p class="row">${gbtn} ${fbtn}</p>
    </form>`)}
  ${card(`<h2>${esc(t("auth.create"))}</h2>
    <p class="muted">${esc(t("auth.createDesc"))}</p>
    <form onsubmit="return doRegister(event)">
      <p><input name="email" placeholder="${esc(t("auth.email"))}" required></p>
      <p><input name="password" type="password"
         placeholder="${esc(t("auth.passwordMin"))}" required minlength="8"></p>
      <p><button type="submit">${esc(t("auth.register"))}</button></p>
    </form>`)}`;
}

// OAuth handoff: backend redirects here with a one-time code in the
// hash fragment; exchange it for the session token (token never in a URL).
async function pageOAuth(hash) {
  const m = /c=([^&]+)/.exec(hash || "");
  if (!m) {
    $app.innerHTML = card(`<p class='warn'>${esc(t("auth.missingCode"))}</p>`);
    return;
  }
  try {
    const d = await api("/api/oauth/exchange", { method: "POST",
      body: JSON.stringify({ code: decodeURIComponent(m[1]) }) });
    setToken(d.token, d.role, d.email);
    location.hash = "#/dashboard";
  } catch (e) {
    $app.innerHTML = card(`<p class="warn">${esc(t("auth.failed"))} ${esc(e.message)}</p>
      <p><a href="#/login">${esc(t("auth.backLogin"))}</a></p>`);
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
      ? `${esc(t("auth.devLink"))} <code>${esc(d.dev_link)}</code>`
      : esc(t("auth.mailSent")), "ok");
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
  let html = `<h1>${esc(t("dash.welcome"))} ${esc(d.email)}</h1>`;
  // next steps — action-oriented, drives the whole business flow
  const steps = [];
  for (const p of d.pending || []) {
    if (p.verify_state === "draft")
      steps.push(`<tr><td>${esc(p.domain)}</td><td><span class="warn">${esc(t("dash.verifyPending"))}</span></td>
        <td><button onclick="verifyDomain(${p.id})">${esc(t("dash.verifyNow"))}</button>
        <span class="muted">${esc(t("dash.verifyHow"))}</span></td></tr>`);
    else if (p.verify_state === "verified")
      steps.push(`<tr><td>${esc(p.domain)}</td><td><span class="warn">${esc(t("dash.awaiting"))}</span></td>
        <td class="muted">${esc(t("dash.reviewNote"))}</td></tr>`);
    else if (p.verify_state === "rejected")
      steps.push(`<tr><td>${esc(p.domain)}</td><td><span class="bad">${esc(t("dash.rejected"))}</span></td>
        <td class="muted">${esc(t("dash.rejectedNote"))}</td></tr>`);
  }
  if (!d.projects.length && !steps.length) {
    steps.push(`<tr><td colspan="3" class="muted">${esc(t("dash.noProject"))}</td></tr>`);
  }
  if (steps.length) {
    html += card(`<h2>${esc(t("dash.nextSteps"))}</h2>
      <table><tr><th>${esc(t("dash.thProject"))}</th><th>${esc(t("dash.thState"))}</th><th>${esc(t("dash.thAction"))}</th></tr>
      ${steps.join("")}</table>`);
  }
  // conversion state — trial credits consumed or window closed
  const exhausted = (d.projects || []).filter(p =>
    p.quota_type === "trial" &&
    (p.quota_used >= p.quota_total ||
     (p.trial_until &&
      new Date(p.trial_until.replace(" ", "T") + "Z") < new Date())));
  if (exhausted.length) {
    html += card(`<h2>${esc(t("dash.exhausted"))}</h2>
      <p>${esc(t("dash.exhaustedBody"))} <b>${esc(exhausted.map(p => p.domain).join(", "))}</b>
      ${esc(t("dash.exhaustedBody2"))}</p>
      <p><a class="btn" href="#/pricing">${esc(t("dash.choosePackage"))}</a></p>`);
  }
  // free-trial withheld — the guards denied credits (see guard.py); the
  // project still runs, it just starts at 0 and needs a package.
  const denyKey = {client_used: "trial.deny.used", email_reused: "trial.deny.email",
                   site_reused: "trial.deny.site", site_claimed: "trial.deny.claimed",
                   ip_reused: "trial.deny.ip"};
  const withheld = (d.projects || []).filter(p => p.quota_total === 0 && denyKey[p.trial_note]);
  if (withheld.length) {
    html += card(`<h2>${esc(t("dash.withheld"))}</h2>
      <p>${esc(t("dash.withheldBody"))}</p>
      <ul class="muted">${withheld.map(p => `<li><b>${esc(p.domain)}</b> — ${esc(t(denyKey[p.trial_note]))}</li>`).join("")}</ul>
      <p>${esc(t("dash.withheldCta"))}</p>
      <p><a class="btn" href="#/pricing">${esc(t("dash.choosePackage"))}</a></p>`);
  }
  if (!d.projects.length) {
    html += card(`<p class="muted">${esc(t("dash.noActive"))}
      <a href="#/new">${esc(t("dash.createProject"))}</a>${esc(t("dash.createProject2"))}</p>`);
    $app.innerHTML = html; return;
  }
  // quota — progress bar, remaining credits, trial countdown
  const quotaRows = d.projects.map(p => {
    const pct = p.quota_total ? Math.min(100, Math.round(100 * p.live / p.quota_total)) : 0;
    const remain = Math.max(0, p.quota_total - p.quota_used);
    let trial = "";
    if (p.quota_type === "trial" && p.trial_until) {
      const days = Math.ceil((new Date(p.trial_until.replace(" ", "T") + "Z") - Date.now()) / 86400000);
      trial = days > 0 ? `<span class="ok">${days}${esc(t("dash.daysLeft"))}</span>`
                       : `<span class="bad">${esc(t("dash.windowClosed"))}</span>`;
    } else if (p.quota_total === 0 && denyKey[p.trial_note]) {
      trial = `<span class="bad">${esc(t("dash.withheldTag"))}</span>`;
    }
    return `<tr><td>${esc(p.domain)}</td><td>${esc(p.quota_type)}</td>
      <td><div class="qbar ${pct >= 100 ? "full" : ""}"><i style="width:${pct}%"></i></div>
        <small class="muted">${pct}%</small></td>
      <td>${p.quota_used} ${esc(t("dash.used"))} · ${remain} ${esc(t("dash.left"))}</td>
      <td>${trial || "—"}</td>
      <td>${p.live} / ${p.quota_total}</td>
      <td>${p.live_month}</td>
      <td>${p.inflight}</td><td>${p.pending}</td><td>${p.live30}</td></tr>`;
  }).join("");
  html += card(`<h2>${esc(t("dash.quota"))}</h2>
    <table><tr><th>${esc(t("dash.thProject"))}</th><th>${esc(t("dash.thType"))}</th><th>${esc(t("dash.thConsumed"))}</th>
    <th>${esc(t("dash.thCredits"))}</th><th>${esc(t("dash.thTrial"))}</th><th>${esc(t("dash.thLiveQuota"))}</th>
    <th>${esc(t("dash.thLiveMonth"))}</th><th>${esc(t("dash.thInflight"))}</th><th>${esc(t("dash.thPending"))}</th><th>${esc(t("dash.thLive30"))}</th></tr>
    ${quotaRows}</table>
    <p class="muted">${esc(t("dash.quotaNote"))} <a href="#/pricing">${esc(t("nav.pricing"))} →</a></p>`);
  html += card(`<h2>${esc(t("dash.capacity"))}</h2>
    ${d.capacity.length ? `<table><tr><th>${esc(t("dash.thPlacement"))}</th><th>${esc(t("dash.thAddresses"))}</th></tr>
      ${d.capacity.map(c => `<tr><td>${esc(c.placement)}</td><td>${c.used} / 100</td></tr>`).join("")}
      </table>` : `<p class="muted">${esc(t("dash.nothingToday"))}</p>`}
    <p class="muted">${esc(t("dash.budgetNote"))}</p>`);
  html += card(`<h2>${esc(t("dash.ledger"))}</h2>
    ${d.ledger.length ? `<table><tr><th>${esc(t("dash.thProject"))}</th><th>${esc(t("dash.thPlacement"))}</th><th>DR</th><th>${esc(t("dash.thConfirmed"))}</th></tr>
      ${d.ledger.map(l => `<tr><td>${esc(l.project)}</td><td>${esc(l.placement)}</td>
        <td>${l.dr == null ? "—" : Math.round(l.dr)}</td><td>${esc(l.checked_at)}</td></tr>`).join("")}
      </table>` : `<p class="muted">${esc(t("dash.noLive"))}</p>`}
    <p class="muted">${esc(t("dash.evidence"))}
    <a href="#/reports">${esc(t("dash.weekly"))}</a></p>`);
  const b = d.dr_buckets;
  html += card(`<h2>${esc(t("dash.quality"))}</h2>
    <p>${t("dash.drDist")} <b>${b["<30"]}</b>, 30–60: <b>${b["30-60"]}</b>,
    60+: <b>${b["60+"]}</b>, ${esc(t("dash.unranked"))} <b>${b.unknown}</b></p>
    <table><tr><th>${esc(t("dash.thSource"))}</th><th>${esc(t("dash.thLiveLinks"))}</th></tr>
    ${d.source_mix.map(m => `<tr><td>${esc(m.stype)}</td><td>${m.n}</td></tr>`).join("") ||
      `<tr><td class='muted'>${esc(t("dash.noData"))}</td></tr>`}</table>`);
  html += card(`<h2>${esc(t("dash.funnel"))}</h2>
    <table><tr><th>${esc(t("dash.thWeek"))}</th><th>${esc(t("dash.thSubmitted"))}</th><th>${esc(t("dash.thConfirmedLive"))}</th><th>${esc(t("dash.thRate"))}</th></tr>
    ${d.funnel.map(w => `<tr><td>${esc(w.week)}</td><td>${w.submitted}</td>
      <td>${w.live}</td><td>${w.rate}</td></tr>`).join("")}</table>`);
  $app.innerHTML = html;
}

// ---------------------------------------------------------------- reports
async function pageReports() {
  requireAuth();
  const d = await api("/api/reports");
  let html = `<h1>${esc(t("rep.title"))}</h1>`;
  html += d.reports.length
    ? d.reports.map(r => card(`<h2>${esc(r.domain)}</h2><pre>${esc(r.text)}</pre>`)).join("")
    : card(`<p class='muted'>${esc(t("rep.none"))}</p>`);
  $app.innerHTML = html;
}

// ---------------------------------------------------------------- new project
async function pageNew() {
  requireAuth();
  const d = await api("/api/requests");
  let reqs = "";
  for (const r of d.requests) {
    if (r.verify_state === "draft") {
      reqs += card(`<b>${esc(r.domain)}</b> <span class="muted">${esc(t("new.pendingVerify"))}</span>
        <p class="muted">${t("new.tagHint")}</p>
        <p><code>&lt;meta name='osched-verify' content='${esc(r.verify_token)}'&gt;</code></p>
        <button onclick="verifyDomain(${r.id})">${esc(t("new.verifyNow"))}</button>`);
    } else {
      reqs += card(`<b>${esc(r.domain)}</b>
        <span class="ok">${esc(r.verify_state)}</span>
        <span class="muted">${esc(t("dash.thCredits"))} ${r.quota_used}/${r.quota_total} (${esc(r.quota_type)})</span>`);
    }
  }
  $app.innerHTML = `<h1>${esc(t("new.title"))}</h1>
    ${d.trial_used ? "" : `<p class="ok">${esc(t("new.trialOk"))}</p>`}
    <div id="notice"></div>
    ${card(`<form onsubmit="return doNew(event)">
      <p><input name="domain" placeholder="${esc(t("new.domain"))}" required></p>
      <p><input name="site_title" placeholder="${esc(t("new.siteTitle"))}" required></p>
      <p><textarea name="site_summary" rows="3"
         placeholder="${esc(t("new.summary"))}"></textarea></p>
      <p><input name="contact_email" placeholder="${esc(t("new.contact"))}" required></p>
      <p><input name="tags" placeholder="${esc(t("new.tags"))}"></p>
      <p><button type="submit">${esc(t("new.create"))}</button></p>
    </form>`)}
    <h2>${esc(t("new.yours"))}</h2>${reqs || `<p class="muted">${esc(t("new.none"))}</p>`}`;
}
window.doNew = async ev => {
  ev.preventDefault();
  const f = ev.target;
  try {
    await api("/api/new", { method: "POST", body: JSON.stringify({
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
  if (me.role !== "admin") { $app.innerHTML = card(`<p class='warn'>${esc(t("adm.adminsOnly"))}</p>`); return; }
  const d = await api("/api/admin");
  let html = `<h1>${esc(t("adm.title"))}</h1><div id="notice"></div>`;
  html += card(`<h2>${esc(t("adm.whitelist"))}</h2>
    <p>${d.whitelist.map(w => `<code>${esc(w)}</code>`).join(" ") ||
      `<span class='muted'>${esc(t("adm.empty"))}</span>`}</p>
    <p class="muted">${esc(t("adm.whitelistNote"))}</p>
    <form onsubmit="return doAdmin(event,'whitelist')" class="row">
      <input name="email" placeholder="${esc(t("adm.addEmail"))}">
      <button>${esc(t("adm.add"))}</button></form>`);
  html += card(`<h2>${esc(t("adm.pending"))}</h2>
    <table><tr><th>${esc(t("adm.thClient"))}</th><th>${esc(t("adm.thDomain"))}</th><th>${esc(t("adm.thState"))}</th><th></th></tr>
    ${d.drafts.map(r => `<tr><td>${esc(r.email)}</td><td>${esc(r.domain)}</td>
      <td>${esc(r.verify_state)}</td>
      <td><button onclick="doAdminBtn('approve',${r.id})">${esc(t("adm.approve"))}</button></td></tr>`).join("") ||
      `<tr><td colspan='4' class='muted'>${esc(t("adm.noPending"))}</td></tr>`}</table>`);
  html += card(`<h2>${t("adm.actives")}</h2>
    <table><tr><th>${esc(t("adm.thProject"))}</th><th>${esc(t("adm.thClient"))}</th><th>${esc(t("adm.thType"))}</th><th>${esc(t("adm.thUsedTotal"))}</th><th>${esc(t("adm.thSetTotal"))}</th></tr>
    ${d.actives.map(r => `<tr><td>${esc(r.domain)}</td><td>${esc(r.email)}</td>
      <td>${esc(r.quota_type)}</td><td>${r.quota_used} / ${r.quota_total}</td>
      <td><form onsubmit="return doAdmin(event,'quota',${r.id})" class="row">
        <input name="quota_total" value="${r.quota_total}" style="width:70px">
        <button>${esc(t("adm.set"))}</button></form></td></tr>`).join("") ||
      `<tr><td colspan='5' class='muted'>${esc(t("adm.noActive"))}</td></tr>`}</table>`);
  html += card(`<h2>${esc(t("adm.packages"))}</h2>
    <table><tr><th>${esc(t("adm.thClient"))}</th><th>${esc(t("adm.thTier"))}</th><th>${esc(t("adm.thPlanned"))}</th><th>${esc(t("adm.thPrice"))}</th><th></th></tr>
    ${(d.packages || []).map(p => `<tr><td>${esc(p.email)}</td>
      <td>${esc(p.tier)}</td><td>${p.links_planned || "—"}</td>
      <td>$${p.price_per_link}</td>
      <td class="row"><button onclick="doAdminBtn('package_done',${p.id})">${esc(t("adm.done"))}</button>
      <button onclick="doAdminBtn('package_cancel',${p.id})">${esc(t("adm.cancel"))}</button></td></tr>`).join("") ||
      `<tr><td colspan='5' class='muted'>${esc(t("adm.noPackages"))}</td></tr>`}</table>
    <p class="muted">${esc(t("adm.packageNote"))}</p>`);
  html += card(`<h2>${esc(t("adm.clients"))}</h2>
    <table><tr><th>${esc(t("adm.thEmail"))}</th><th>${esc(t("adm.thRole"))}</th><th></th></tr>
    ${d.clients.map(c => `<tr><td>${esc(c.email)}</td><td>${esc(c.role)}</td>
      <td><button onclick="doAdminBtn('whitelist',null,'${esc(c.email)}')">
        ${d.whitelist.includes(c.email) ? esc(t("adm.remove")) : esc(t("adm.whitelistBtn"))}</button></td></tr>`).join("")}</table>`);
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
  if (!token) { location.hash = "#/login"; throw new Error(t("err.loginFirst")); }
}

function refreshMe() {
  me = token ? { role: localStorage.getItem("portal_role"),
                 email: localStorage.getItem("portal_email") } : null;
}
document.documentElement.lang = lang === "zh" ? "zh-Hans" : "en";
refreshMe();
render();
