#!/usr/bin/env python3
"""Internship watcher: polls intern lists + company job boards, pushes new matches to your phone via ntfy.

Usage:
  python3 watcher.py            # normal run: notify about postings not seen before
  python3 watcher.py --dry-run  # print what would be sent, don't notify or save state

Env:
  NTFY_TOPIC   ntfy.sh topic to push to (or put it in a local .ntfy_topic file)
"""
import csv, json, os, re, ssl, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen.json"
LOG_FILE = HERE / "jobs_log.csv"
DIGEST_FILE = HERE / "open_now.md"
SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"

WANTED_TERMS = {"Summer 2027", "Fall 2027", "Winter 2027", "Spring 2027", "Summer 2028", "N/A"}
WANTED_CATEGORIES = {"Software", "AI/ML/Data", "Software Engineering", "Data Science, AI & Machine Learning"}
MAX_PUSHES_PER_RUN = 12

INTERN_RE = re.compile(r"\b(intern|internship|co-?op|student|placement)\b", re.I)
ROLE_RE = re.compile(r"software|developer|engineer|\bswe\b|machine learning|\bml\b|\bai\b|artificial intelligence|"
                     r"data|backend|back-end|full[- ]?stack|platform|infrastructure|llm|nlp|computer vision|mlops", re.I)
EXCLUDE_RE = re.compile(r"\bph\.?d|doctoral|master'?s only|\bmba\b|senior|staff|principal|hardware|mechanical|electrical|"
                        r"sales|marketing|recruit|finance|accounting|legal|design intern|\bhr\b", re.I)
CANADA_RE = re.compile(r"(?i:canada|toronto|ontario|waterloo|kitchener|ottawa|mississauga|markham|vaughan|montr[eé]al|"
                       r"qu[eé]bec|vancouver|british columbia|calgary|edmonton|alberta|halifax|winnipeg)|\b(ON|BC|QC|AB)\b")
NA_RE = re.compile(r"united states|\bUSA?\b|remote|, [A-Z]{2}\b|new york|san francisco|seattle|boston|austin|chicago", re.I)
STRONG_RE = re.compile(r"machine learning|\bml\b|\bai\b|llm|nlp|data engineer|applied scien|computer vision|mlops|genai", re.I)


try:  # python.org builds on macOS ship without CA certs; use certifi if available
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()


def fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "intern-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.load(r)


def job(source, jid, company, title, location, url, terms=""):
    return {"key": f"{source}:{jid}", "company": company.strip(), "title": title.strip(),
            "location": location.strip(), "url": url, "terms": terms}


# ---------- sources ----------
def from_simplify():
    out = []
    for x in fetch_json(SIMPLIFY_URL):
        if not (x.get("active") and x.get("is_visible")):
            continue
        if x.get("category") not in WANTED_CATEGORIES or not WANTED_TERMS & set(x.get("terms") or []):
            continue
        if x.get("sponsorship") == "U.S. Citizenship is Required":
            continue
        degrees = set(x.get("degrees") or [])
        if degrees and "Bachelor's" not in degrees:
            continue
        locs = "; ".join(x.get("locations") or [])
        if locs and not (CANADA_RE.search(locs) or NA_RE.search(locs)):
            continue
        out.append(job("simplify", x["id"], x["company_name"], x["title"], locs, x["url"],
                       ", ".join(t for t in x["terms"] if t != "N/A")))
    return out


def from_greenhouse(slug):
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [job("gh", j["id"], slug.title(), j["title"], (j.get("location") or {}).get("name", ""), j["absolute_url"])
            for j in data.get("jobs", [])]


def from_ashby(slug):
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in data.get("jobs", []):
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        out.append(job("ashby", j["id"], slug.title(), j["title"], "; ".join(l for l in locs if l), j["jobUrl"]))
    return out


def from_lever(slug):
    data = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [job("lever", j["id"], slug.title(), j["text"], (j.get("categories") or {}).get("location", "") or "",
                j["hostedUrl"]) for j in data]


def ats_filter(j):
    t = j["title"]
    if not (INTERN_RE.search(t) and ROLE_RE.search(t)) or EXCLUDE_RE.search(t):
        return False
    return not j["location"] or bool(CANADA_RE.search(j["location"]) or NA_RE.search(j["location"]))


def collect():
    companies = json.loads((HERE / "companies.json").read_text())
    tasks = [("simplify", from_simplify, None)]
    for kind, fn in (("greenhouse", from_greenhouse), ("ashby", from_ashby), ("lever", from_lever)):
        tasks += [(f"{kind}/{s}", fn, s) for s in companies.get(kind, [])]

    def run(t):
        name, fn, arg = t
        try:
            jobs = fn(arg) if arg else fn()
            return jobs if name == "simplify" else [j for j in jobs if ats_filter(j)]
        except Exception as e:  # one broken board must not kill the run
            print(f"[warn] {name}: {e}", file=sys.stderr)
            return []

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(run, tasks))
    jobs, seen_keys = [], set()
    for batch in results:
        for j in batch:
            if j["key"] not in seen_keys and not EXCLUDE_RE.search(j["title"]):
                seen_keys.add(j["key"])
                jobs.append(j)
    return jobs


# ---------- ranking / notify ----------
def score(j):
    s = 0
    if CANADA_RE.search(j["location"]): s += 3
    if re.search(r"toronto|remote", j["location"], re.I): s += 1
    if STRONG_RE.search(j["title"]): s += 2
    return s


def flag(j):
    return "🇨🇦 " if CANADA_RE.search(j["location"]) else ""


def ntfy_topic():
    t = os.environ.get("NTFY_TOPIC", "").strip()
    f = HERE / ".ntfy_topic"
    return t or (f.read_text().strip() if f.exists() else "")


def push(topic, title, body, click=None, priority="default", tags="briefcase"):
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=15, context=SSL_CTX).read()


def write_digest(jobs):
    jobs = sorted(jobs, key=lambda j: (-score(j), j["company"].lower()))
    lines = [f"# Open internships matching your profile ({len(jobs)})", "",
             f"_Snapshot {datetime.now():%Y-%m-%d %H:%M}. Canada + AI/ML/data roles first. New postings after this go to your phone._", "",
             "| | Company | Role | Location | Term |", "|---|---|---|---|---|"]
    for j in jobs:
        loc = j["location"].replace("|", "/")[:60]
        lines.append(f"| {flag(j).strip()} | {j['company']} | [{j['title']}]({j['url']}) | {loc} | {j['terms']} |")
    DIGEST_FILE.write_text("\n".join(lines) + "\n")


def log(jobs):
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["found_at_utc", "company", "title", "location", "terms", "url"])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        for j in jobs:
            w.writerow([now, j["company"], j["title"], j["location"], j["terms"], j["url"]])


def main():
    dry = "--dry-run" in sys.argv
    jobs = collect()
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    first_run = not seen
    new = [j for j in jobs if j["key"] not in seen]
    print(f"{len(jobs)} matching postings, {len(new)} new{' (first run: seeding)' if first_run else ''}")

    if first_run:
        write_digest(jobs)
        print(f"Wrote {DIGEST_FILE.name} with everything open right now.")
    else:
        new.sort(key=lambda j: -score(j))
        topic = ntfy_topic()
        for j in new[:MAX_PUSHES_PER_RUN]:
            title = f"{flag(j)}{j['company']}: {j['title']}"[:120]
            body = f"{j['location'] or 'Location n/a'}" + (f" · {j['terms']}" if j["terms"] else "") + "\nTap to apply."
            pri = "high" if score(j) >= 3 else "default"
            print(" NEW", title, "|", j["url"])
            if not dry and topic:
                try:
                    push(topic, title, body, j["url"], pri)
                except Exception as e:
                    print(f"[warn] push failed: {e}", file=sys.stderr)
        extra = len(new) - MAX_PUSHES_PER_RUN
        if extra > 0 and not dry and topic:
            push(topic, f"+{extra} more new internships", "See jobs_log.csv in your job-watcher repo.", tags="inbox_tray")
        if not dry and not topic and new:
            print("[warn] no NTFY_TOPIC set; nothing pushed", file=sys.stderr)

    if not dry:
        if new and not first_run:
            log(new)
        now = int(time.time())
        for j in new:
            seen[j["key"]] = now
        cutoff = now - 120 * 86400  # forget keys after 120 days to keep the file small
        live = {j["key"] for j in jobs}
        seen = {k: v for k, v in seen.items() if v >= cutoff or k in live}
        SEEN_FILE.write_text(json.dumps(seen, indent=0, sort_keys=True))


if __name__ == "__main__":
    main()
