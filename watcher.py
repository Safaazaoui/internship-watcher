#!/usr/bin/env python3
"""Internship watcher: polls intern lists + company job boards, pushes new matches to your phone via ntfy.

Usage:
  python3 watcher.py            # normal run: notify about postings not seen before
  python3 watcher.py --dry-run  # print what would be sent, don't notify or save state

Env:
  NTFY_TOPIC   ntfy.sh topic to push to (or put it in a local .ntfy_topic file)
"""
import csv, html, json, os, re, ssl, sys, time, urllib.request
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
REMOTE_RE = re.compile(r"remote|anywhere|distributed", re.I)

# US-only roles a Canadian student can't take: citizenship / clearance / no-sponsorship language
US_BLOCK_RE = re.compile(r"u\.?s\.? citizen|united states citizen|us citizenship|citizenship is required|security clearance|"
                         r"clearance required|ts/sci|secret clearance|\bitar\b|export control|green card|"
                         r"permanent resident of the united states|without (?:the need for )?(?:visa |employer )?sponsorship|"
                         r"(?:will not|won't|unable to|cannot|does not|do not) (?:provide |offer )?sponsor", re.I)
# Defense / government contractors: US intern roles almost always need US citizenship
US_ONLY_EMPLOYERS = re.compile(r"\b(rtx|raytheon|lockheed|northrop|general dynamics|l3harris|bae systems|leidos|"
                               r"booz allen|saic|anduril|boeing|huntington ingalls|parsons|caci|mitre|sandia|"
                               r"lawrence livermore|los alamos|oak ridge|jhu ?apl|johns hopkins applied|mantech|"
                               r"peraton|textron|collins aerospace|pratt & whitney|shield ai|palantir usg)\b"
                               r"|university|college|\bschool of\b", re.I)  # US campus jobs are for their own students


try:  # python.org builds on macOS ship without CA certs; use certifi if available
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()


def fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "intern-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.load(r)


def to_date(v):
    """Epoch seconds/ms or ISO string -> 'YYYY-MM-DD' ('' if unknown)."""
    if not v:
        return ""
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc).strftime("%Y-%m-%d")
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, OSError):
        return ""


def job(source, jid, company, title, location, url, terms="", posted="", text=""):
    return {"key": f"{source}:{jid}", "company": company.strip(), "title": title.strip(),
            "location": location.strip(), "url": url, "terms": terms, "posted": to_date(posted), "text": text}


def region(j):
    """'ca' (Canada), 'us_remote', 'us' or 'other'."""
    loc = j["location"]
    if CANADA_RE.search(loc):
        return "ca"
    if not loc or REMOTE_RE.search(loc):
        return "us_remote"
    return "us" if NA_RE.search(loc) else "other"


def open_to_canadians(j):
    """Canada roles always pass; US roles are dropped if they need citizenship/clearance or won't sponsor."""
    if region(j) == "ca":
        return True
    if US_ONLY_EMPLOYERS.search(j["company"]):
        return False
    return not US_BLOCK_RE.search(j["title"] + " " + j.get("text", ""))


# ---------- sources ----------
def from_simplify():
    out = []
    for x in fetch_json(SIMPLIFY_URL):
        if not (x.get("active") and x.get("is_visible")):
            continue
        if x.get("category") not in WANTED_CATEGORIES or not WANTED_TERMS & set(x.get("terms") or []):
            continue
        if x.get("sponsorship") in ("U.S. Citizenship is Required", "Does Not Offer Sponsorship") \
                and not CANADA_RE.search(" ".join(x.get("locations") or [])):
            continue
        degrees = set(x.get("degrees") or [])
        if degrees and "Bachelor's" not in degrees:
            continue
        locs = "; ".join(x.get("locations") or [])
        if locs and not (CANADA_RE.search(locs) or NA_RE.search(locs)):
            continue
        out.append(job("simplify", x["id"], x["company_name"], x["title"], locs, x["url"],
                       ", ".join(t for t in x["terms"] if t != "N/A"), x.get("date_posted")))
    return out


def from_greenhouse(slug):
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [job("gh", j["id"], j.get("company_name") or slug.title(), j["title"],
                (j.get("location") or {}).get("name", ""), j["absolute_url"],
                posted=j.get("first_published") or j.get("updated_at"), text=html.unescape(j.get("content") or ""))
            for j in data.get("jobs", [])]


def from_ashby(slug):
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in data.get("jobs", []):
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        out.append(job("ashby", j["id"], slug.title(), j["title"], "; ".join(l for l in locs if l), j["jobUrl"],
                       posted=j.get("publishedAt"), text=j.get("descriptionPlain") or ""))
    return out


def from_lever(slug):
    data = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        lists = " ".join(" ".join([l.get("text", ""), l.get("content", "")]) for l in j.get("lists") or [])
        out.append(job("lever", j["id"], slug.title(), j["text"], (j.get("categories") or {}).get("location", "") or "",
                       j["hostedUrl"], posted=j.get("createdAt"),
                       text=" ".join([j.get("descriptionPlain") or "", lists, j.get("additionalPlain") or ""])))
    return out


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
            if j["key"] not in seen_keys and not EXCLUDE_RE.search(j["title"]) and region(j) != "other" \
                    and open_to_canadians(j):
                seen_keys.add(j["key"])
                jobs.append(j)
    return jobs


# ---------- ranking / notify ----------
REGION_LABEL = {"ca": "🇨🇦 Canada", "us_remote": "🇺🇸 US remote", "us": "🇺🇸 US on-site"}
REGION_ORDER = {"ca": 0, "us_remote": 1, "us": 2}


def score(j):
    r, s = region(j), 0
    s += {"ca": 3, "us_remote": 2, "us": 0}.get(r, 0)
    if re.search(r"toronto|remote in canada", j["location"], re.I): s += 1
    if STRONG_RE.search(j["title"]): s += 2
    return s


def flag(j):
    return {"ca": "🇨🇦 ", "us_remote": "🇺🇸🏠 ", "us": "🇺🇸 "}.get(region(j), "")


def pretty_date(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d") if d else "date n/a"


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
    lines = [f"# Open internships matching your profile ({len(jobs)})", "",
             f"_Updated {datetime.now(timezone.utc):%Y-%m-%d} (UTC). US roles needing US citizenship, clearance or "
             "\"no sponsorship\" are filtered out. Within each section: AI/ML/data roles first, then newest._", ""]
    for r in ("ca", "us_remote", "us"):
        group = [j for j in jobs if region(j) == r]
        group.sort(key=lambda j: (-score(j), j["posted"] == "", "".join(chr(255 - ord(c)) for c in j["posted"])))
        lines += [f"## {REGION_LABEL[r]} ({len(group)})", "",
                  "| Posted | Company | Role | Location | Term |", "|---|---|---|---|---|"]
        for j in group:
            loc = j["location"].replace("|", "/")[:60]
            lines.append(f"| {j['posted'] or '–'} | {j['company']} | [{j['title']}]({j['url']}) | {loc} | {j['terms']} |")
        lines.append("")
    DIGEST_FILE.write_text("\n".join(lines))


def log(jobs):
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["found_at_utc", "posted", "region", "company", "title", "location", "terms", "url"])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        for j in jobs:
            w.writerow([now, j["posted"], region(j), j["company"], j["title"], j["location"], j["terms"], j["url"]])


def main():
    dry = "--dry-run" in sys.argv
    jobs = collect()
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    first_run = not seen
    new = [j for j in jobs if j["key"] not in seen]
    fresh_keys = [j["key"] for j in new]  # includes ones skipped below, so they aren't re-checked every run
    print(f"{len(jobs)} matching postings, {len(new)} new{' (first run: seeding)' if first_run else ''}")

    if not dry or first_run:
        write_digest(jobs)
    if not first_run and new:
        # Intern-list entries have no description, so open each new US posting and scan it for citizenship/clearance text
        def page_ok(j):
            if j["key"].startswith("simplify:") and region(j) != "ca":
                try:
                    req = urllib.request.Request(j["url"], headers={"User-Agent": "Mozilla/5.0"})
                    page = urllib.request.urlopen(req, timeout=12, context=SSL_CTX).read(600_000).decode("utf-8", "ignore")
                    if US_BLOCK_RE.search(html.unescape(re.sub(r"<[^>]+>", " ", page))):
                        print(" skip (US-only):", j["company"], "-", j["title"])
                        return False
                except Exception:
                    pass  # page unreachable or JS-only (e.g. Workday): keep it
            return True
        with ThreadPoolExecutor(8) as ex:
            keep = list(ex.map(page_ok, new))
        new_to_send = [j for j, ok in zip(new, keep) if ok]
    else:
        new_to_send = []

    if not first_run:
        new = new_to_send
        new.sort(key=lambda j: -score(j))
        topic = ntfy_topic()
        for j in new[:MAX_PUSHES_PER_RUN]:
            title = f"{flag(j)}{j['company']}: {j['title']}"[:120]
            body = (f"Posted {pretty_date(j['posted'])} · {j['location'] or 'Location n/a'}"
                    + (f" · {j['terms']}" if j["terms"] else "") + "\nTap to apply.")
            pri = "high" if score(j) >= 3 else "default"
            print(" NEW", title, "|", j["url"])
            if not dry and topic:
                try:
                    push(topic, title, body, j["url"], pri)
                except Exception as e:
                    print(f"[warn] push failed: {e}", file=sys.stderr)
        extra = len(new) - MAX_PUSHES_PER_RUN
        if extra > 0 and not dry and topic:
            try:
                push(topic, f"+{extra} more new internships", "See open_now.md / jobs_log.csv in your repo.", tags="inbox_tray")
            except Exception as e:
                print(f"[warn] push failed: {e}", file=sys.stderr)
        if not dry and not topic and new:
            print("[warn] no NTFY_TOPIC set; nothing pushed", file=sys.stderr)

    if not dry:
        if new and not first_run:
            log(new)
        now = int(time.time())
        for k in fresh_keys:
            seen[k] = now
        cutoff = now - 120 * 86400  # forget keys after 120 days to keep the file small
        live = {j["key"] for j in jobs}
        seen = {k: v for k, v in seen.items() if v >= cutoff or k in live}
        SEEN_FILE.write_text(json.dumps(seen, indent=0, sort_keys=True))


if __name__ == "__main__":
    main()
