# Internship watcher

Two parts:

1. **`watcher.py` (fast, runs in the cloud on GitHub Actions).** It polls the SimplifyJobs 2027 intern list (thousands of roles, updated many times a day) and the company job boards in `companies.json` (Greenhouse, Ashby, Lever). New AI, software or data internships in Canada or the US get pushed to your phone through [ntfy](https://ntfy.sh). Canadian roles and AI/ML roles are sent as high priority (🇨🇦 = Canada).
2. **Claude sweep (twice a day, 7:30 AM and 6:30 PM, on your Mac).** Claude searches the web for postings the script can't see (bank and Workday career sites, LinkedIn, Indeed, government programs), scores each one 1–10 against your resume, alerts you about anything scoring 7 or more, and writes `sweep_reports/<date>.md`. It only runs while the Claude app is open.

## Phone setup (2 min)
1. Install the **ntfy** app (iOS or Android).
2. Tap **+** and subscribe to the topic stored in the local file `.ntfy_topic`. Anyone who knows the topic name can read it, so don't share it.

## Files
- `open_now.md`: everything open right now that matches you. Start applying here today.
- `jobs_log.csv`: every new posting found since setup.
- `seen.json`: postings already alerted, so you don't get repeats.
- `companies.json`: add companies you care about. The slug comes from the careers URL: `boards.greenhouse.io/<slug>`, `jobs.ashbyhq.com/<slug>` or `jobs.lever.co/<slug>`.

Run it locally: `python3 watcher.py --dry-run`
