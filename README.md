# Turf Console — Setup & Updating

A weekly fescue brief. Runs free in the cloud, emails you every Sunday, publishes a
dashboard you bookmark. No device of yours needs to be on; no Claude account involved.

## The model: your plan vs. the engine
- **lawn.yaml** = YOUR PROGRAM. Location, lawn facts, irrigation tunables, equipment,
  products + rates, and your seasonal schedule. This is the file you edit.
- **log.yaml** = WHAT YOU DID / SAW. Mowing, applications, and issues you're watching.
  Appended by the "Log" button (you can hand-edit too).
- **turf_brief.py** = the engine. Reads both files, pulls weather, builds the brief.
  You rarely touch this.

So "updating my schedule, rates, products, issues" never means editing code. It means
editing one readable file, or tapping a button.

---

## HOW TO UPDATE THINGS (the part you asked about)

### Change your plan — schedule, products, rates, irrigation targets, location
Edit **lawn.yaml**. On your phone or iPad: open the repo on github.com → tap `lawn.yaml`
→ pencil icon → edit → "Commit changes". It's plain text with comments explaining each
field. Examples:
- New product or corrected rate → edit the `products:` list.
- Move a fertilizer window two weeks earlier → change the `start:`/`end:` dates under
  `schedule:`.
- Bump your summer water target or set your sprinkler rate → edit `irrigation:`.
- Your comments are never wiped — the Log button only touches log.yaml.

### Log what you did, or an issue you see — the fast path
Don't edit a file. GitHub app (or website) → **Actions** tab → **"Log a Lawn Action"** →
**Run workflow**. Pick the type (Mowed, Fungicide, Observation/issue, Close an issue…),
type an optional detail, submit. It appends to log.yaml and commits for you.
- "Mowed" automatically updates the date that drives the best-mow-day logic.
- "Observation / issue" adds it to the **Watching** section of your brief until you close it.
- "Close an issue" clears it (type a word from the note to target a specific one).

### Big changes — let Claude rewrite it
For anything chunky ("redo my whole fall schedule", "add these 8 products with rates"),
paste lawn.yaml into a Claude chat, describe the change, and commit the version it hands back.

---

## One-time setup (~12 min, free)

Free GitHub Pages needs a **public** repo. The setup keeps everything personal out of the
code (password + emails are secrets; location is town-level), so a public repo holds only
lawn math. (Prefer private? GitHub Pro is ~$4/mo and changes nothing else.)

1. **Gmail App Password** — turn on 2-Step Verification (Google Account → Security), then
   App Passwords → create "Turf", copy the 16-char code.
2. **Edit lawn.yaml** — set your real values; leave EMAIL fields in turf_brief.py blank.
3. **Public repo** (e.g. `turf-console`). Add at the root: `turf_brief.py`, `lawn.yaml`,
   `log.yaml`. Add the two workflows under `.github/workflows/`: `turf-brief.yml` and `log.yml`.
4. **Secrets** — repo → Settings → Secrets and variables → Actions. Add:
   `TURF_SMTP_PASS` (app password), `TURF_SMTP_USER` (your Gmail), `TURF_SMTP_TO` (where to send).
5. **Pages** — Settings → Pages → Source = **GitHub Actions**.
6. **Run it** — Actions → "Turf Console Weekly Brief" → Run workflow. Check your inbox; the
   run log prints your dashboard URL (`https://YOURNAME.github.io/turf-console/`). Bookmark it.

Done. Brief emails every Sunday, dashboard self-updates, schedule stays alive because the
Log button's commits count as activity.

## Notes
- Weather: COMPLETED days use observed data (Open-Meteo archive / ERA5 reanalysis); today
  and the forecast use the forecast endpoint. So the trailing water balance is what actually
  fell, not a forecast of the past. The last ~1-2 days (reanalysis lag) fall back to the model.
- "How much to apply" is computed from your rates in lawn.yaml × turf_area_sqft, including
  bags-to-buy. Keep rates as `rate_amount`/`rate_unit`/`per_sqft` (+ optional `bag_size`), or
  use `rate_note` for "per label" liquids.
- The script needs `pyyaml`; both workflows install it automatically.
- cron in `turf-brief.yml` is UTC (`0 23 * * 0` ≈ 6 PM Central, summer).
- Photo diagnosis: open Claude in a browser, drop the photo + that week's brief.
- Don't run any of this on the work computer — keep it in the cloud.
