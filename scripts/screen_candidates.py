#!/usr/bin/env python3
"""Screen candidate sites for a genuinely submittable add-site form (§4d).

Why this exists: the pool's `has_submit_form` flag says "some form carries a
URL-ish field", which is true of search boxes, contact handlers and newsletter
widgets. Delivering against such a row produces "submitted" plans whose links
can never appear (pcastuces /soumettre.htm, smoothdirectory's subscribe widget).
This tool answers the question the ledger actually needs: *can an anonymous
visitor put a URL on a page here, with no captcha and no login?*

Output is a ranked JSON report — a screening pass, not a ledger write. Rows are
promoted into the pool only through workbench import (single source of truth).

Usage:
    python3 scripts/screen_candidates.py --domains-file /tmp/doms.txt \
        --out data/screen-2026-11-19.json --workers 12
    python3 scripts/screen_candidates.py e27.co softwaresuggest.com

中文：候选站筛查。判定「匿名访客能否在无验证码、无登录的情况下提交网址」，
输出排序报告；不写台账（入池只走 workbench 导入）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from osched import probe  # noqa: E402

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Browser UA on purpose: the question is "can a human do this", and a bot UA
# gets blocked by WAFs that let humans through.
probe.USER_AGENT = BROWSER_UA

CF_RE = re.compile(
    r"(just a moment|challenges\.cloudflare\.com|cf_chl|"
    r"performing security verification|attention required|checking your browser)",
    re.I,
)
LOGIN_RE = re.compile(r"<input[^>]+type\s*=\s*[\"']password", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

# Pages whose URL says "contact/feedback/support" cannot publish a listing,
# whatever form they carry — the same rule probe.find_submit_form enforces.
CONTACT_PATH_RE = re.compile(
    r"(contact|kontakt|feedback|support|about|erreur|impressum|newsletter)",
    re.I,
)


def _title(html: str) -> str:
    m = TITLE_RE.search(html or "")
    text = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    return text.replace("&#039;", "'").replace("&amp;", "&")[:90]


def _fetch(url: str, timeout: float) -> tuple[int, str, str]:
    try:
        return probe._fetch(url, timeout)
    except Exception as exc:  # noqa: BLE001 - fetch failures are data here
        return 0, url, f"__error__: {exc}"


def screen_one(domain: str, *, timeout: float, max_candidates: int) -> dict:
    row: dict = {
        "domain": domain,
        "home_status": 0,
        "home_title": "",
        "blocked": "",
        "candidates": [],
        "verdict": "unknown",
    }
    status, final_url, html = _fetch(f"https://{domain}/", timeout)
    row["home_status"] = status
    row["home_title"] = _title(html)
    if html.startswith("__error__"):
        row["verdict"] = "unreachable"
        row["error"] = html[:120]
        return row
    if CF_RE.search(html or "") or status in (403, 412, 429, 503):
        row["blocked"] = "cloudflare" if CF_RE.search(html or "") else f"http {status}"
        row["verdict"] = "bot-blocked"
        return row
    if status == 0 or status >= 400:
        row["verdict"] = f"http {status}"
        return row

    cands = probe.find_entry_candidates(html, final_url, max_candidates)
    if probe._looks_like_submit_form(html)[0]:
        cands.insert(0, final_url)

    seen: set[str] = set()
    for cand in cands:
        if cand in seen:
            continue
        seen.add(cand)
        item: dict = {"url": cand, "status": 0, "form": False, "captcha": "unknown",
                      "login_wall": False, "blocked": ""}
        cstatus, cfinal, chtml = _fetch(cand, timeout)
        item["status"] = cstatus
        item["final"] = cfinal
        item["title"] = _title(chtml)
        if chtml.startswith("__error__"):
            item["note"] = "fetch error"
            row["candidates"].append(item)
            continue
        if CF_RE.search(chtml or "") or cstatus in (403, 412, 429, 503):
            item["blocked"] = "cloudflare" if CF_RE.search(chtml or "") else f"http {cstatus}"
        ok, fields = probe._looks_like_submit_form(chtml or "")
        item["form"] = ok
        item["fields"] = fields[:10]
        item["captcha"] = probe._detect_captcha(chtml or "")
        item["login_wall"] = bool(LOGIN_RE.search(chtml or ""))
        item["contact_page"] = bool(CONTACT_PATH_RE.search(cand))
        row["candidates"].append(item)

    good = [c for c in row["candidates"]
            if c.get("form") and not c.get("blocked") and not c.get("login_wall")
            and c.get("captcha") == "none" and not c.get("contact_page")]
    if good:
        good.sort(key=lambda c: (c.get("login_wall"), len(c.get("fields") or [])))
        row["verdict"] = "submittable"
        row["best"] = good[0]["url"]
        row["best_fields"] = good[0]["fields"]
    elif any(c.get("form") for c in row["candidates"]):
        row["verdict"] = "form-but-gated"
        gated = [c for c in row["candidates"] if c.get("form")][0]
        row["gate"] = ("captcha:" + gated["captcha"]) if gated["captcha"] != "none" \
            else ("login" if gated["login_wall"] else
                  ("blocked" if gated["blocked"] else "contact-page"))
    elif row["candidates"]:
        row["verdict"] = "no-form"
    else:
        row["verdict"] = "no-add-site-link"
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("domains", nargs="*", help="domains or URLs to screen")
    ap.add_argument("--domains-file", help="newline-separated domains")
    ap.add_argument("--out", help="write the ranked JSON report here")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--max-candidates", type=int, default=5)
    ap.add_argument("--top", type=int, default=25, help="rows to print")
    ap.add_argument("--retry", type=int, default=1,
                    help="re-screen transient failures (http 0 / unreachable) this many times")
    args = ap.parse_args()

    domains = [d.strip() for d in args.domains if d.strip()]
    if args.domains_file:
        domains += [ln.strip() for ln in Path(args.domains_file).read_text().splitlines()
                    if ln.strip() and not ln.startswith("#")]
    domains = [d.replace("https://", "").replace("http://", "").strip("/").split("/")[0]
               for d in domains]
    domains = list(dict.fromkeys(d for d in domains if d))
    if not domains:
        ap.error("no domains given")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(screen_one, d, timeout=args.timeout,
                               max_candidates=args.max_candidates): d for d in domains}
        for fut in as_completed(futures):
            rows.append(fut.result())

    # Concurrency makes flaky sites look dead. Retry the transient verdicts
    # serially before reporting them as unreachable — a false "dead" verdict
    # costs us a real directory.
    for _ in range(max(0, args.retry)):
        flaky = [r for r in rows if r["verdict"] in ("http 0", "unreachable")]
        if not flaky:
            break
        print(f"retrying {len(flaky)} transient failures")
        by_domain = {r["domain"]: r for r in rows}
        with ThreadPoolExecutor(max_workers=4) as retry_pool:
            futs = {retry_pool.submit(screen_one, r["domain"],
                                      timeout=args.timeout + 6,
                                      max_candidates=args.max_candidates):
                    r["domain"] for r in flaky}
            for fut in as_completed(futs):
                by_domain[futs[fut]] = fut.result()
        rows = list(by_domain.values())

    order = {"submittable": 0, "form-but-gated": 1, "no-form": 2,
             "no-add-site-link": 3, "bot-blocked": 4, "unreachable": 5}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), r["domain"]))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    print(f"screened {len(rows)} domains: " +
          ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for r in rows:
        if r["verdict"] not in ("submittable", "form-but-gated"):
            continue
        if args.top <= 0:
            break
        args.top -= 1
        gate = r.get("gate", "")
        print(f"  {r['verdict']:<15} {r['domain'][:34]:<34} "
              f"{(r.get('best') or '')[:60]:<60} {gate} {len(r.get('best_fields') or [])}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(
            {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
             "counts": counts, "rows": rows}, ensure_ascii=False, indent=1))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
