#!/usr/bin/env python3
"""
TURF CONSOLE — weekly fescue brief.
Reads your program from lawn.yaml (+ log.yaml), pulls weather, prints a brief,
writes an HTML dashboard, optionally emails it.

Weather: COMPLETED days use Open-Meteo's archive (ERA5 reanalysis = observed).
TODAY + future use the forecast endpoint. Recent days the archive hasn't filled yet
fall back to the forecast model. So the trailing water balance is what actually fell.

    python turf_brief.py            # brief + dashboard
    python turf_brief.py --open     # also open it
    python turf_brief.py --email    # also email it (TURF_SMTP_* env vars)

Dependency: pyyaml  (pip install pyyaml)
"""

import json, sys, os, ssl, smtplib, math, webbrowser, urllib.request, urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from email.message import EmailMessage

try:
    import yaml
except ImportError:
    print("Missing pyyaml. Run: pip install pyyaml"); sys.exit(1)

HERE = Path(__file__).parent
PROGRAM_FILE = HERE / "lawn.yaml"
LOG_FILE = HERE / "log.yaml"
HTML_FILE = HERE / "turf-console.html"

EMAIL = {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "sender": "", "recipient": "",
         "password_env": "TURF_SMTP_PASS", "password": ""}

# ============================== LOAD PROGRAM ==============================
def latest_mow(log_entries, fallback):
    mows = [e["date"] for e in log_entries if e.get("type") == "Mowed" and e.get("date")]
    return max(mows) if mows else (fallback or "")

def load_program():
    prog = yaml.safe_load(PROGRAM_FILE.read_text(encoding="utf-8")) or {}
    logd = (yaml.safe_load(LOG_FILE.read_text(encoding="utf-8")) if LOG_FILE.exists() else {}) or {}
    loc, lawn, irr, sup = (prog.get(k, {}) for k in ("location", "lawn", "irrigation", "supply"))
    cfg = {
        "location_name": loc.get("name", "Your Lawn"),
        "lat": loc.get("lat", 38.9108), "lon": loc.get("lon", -94.3822),
        "turf_area_sqft": lawn.get("turf_area_sqft", 8000),
        "hoc_inches": lawn.get("mow_height_in", 4),
        "last_mow_date": latest_mow(logd.get("log", []), lawn.get("last_mowed_fallback", "")),
        "sprinkler_rate_in_per_hr": irr.get("sprinkler_rate_in_per_hr", 0),
        "lead_time_days": sup.get("lead_time_days", 6), "retailer": sup.get("retailer", "the store"),
        "kc": irr.get("crop_coefficient", 0.8), "rootzone": irr.get("rootzone_hold_in", 0.45),
    }
    windows = prog.get("schedule", [])
    products = prog.get("products", [])
    observations = [o for o in logd.get("observations", []) if str(o.get("status", "open")).lower() == "open"]
    recurring = prog.get("recurring_products", [])
    return cfg, windows, products, observations, logd.get("log", []), recurring

# ============================== WEATHER FETCH ==============================
def fetch_forecast(cfg):
    p = {"latitude": cfg["lat"], "longitude": cfg["lon"],
         "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,"
                  "et0_fao_evapotranspiration,precipitation_probability_max,"
                  "wind_speed_10m_max,relative_humidity_2m_max",
         "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
         "wind_speed_unit": "mph", "timezone": "auto", "past_days": 7, "forecast_days": 7}
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)

def fetch_archive(cfg, start, end):
    p = {"latitude": cfg["lat"], "longitude": cfg["lon"],
         "start_date": start, "end_date": end,
         "daily": "precipitation_sum,et0_fao_evapotranspiration",
         "precipitation_unit": "inch", "timezone": "auto"}
    url = "https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)

def _fmap(fc):
    d = fc["daily"]; out = {}
    for i, t in enumerate(d["time"]):
        out[t] = {"precip": d["precipitation_sum"][i], "et0": d["et0_fao_evapotranspiration"][i],
                  "tmax": d["temperature_2m_max"][i], "tmin": d["temperature_2m_min"][i],
                  "rh": d["relative_humidity_2m_max"][i], "pop": d["precipitation_probability_max"][i],
                  "wind": d["wind_speed_10m_max"][i]}
    return out

def _amap(ar):
    if not ar or "daily" not in ar:
        return {}
    d = ar["daily"]; out = {}
    for i, t in enumerate(d["time"]):
        out[t] = {"precip": d["precipitation_sum"][i], "et0": d["et0_fao_evapotranspiration"][i]}
    return out

def assemble(forecast_json, archive_json, cfg):
    fmap = _fmap(forecast_json)
    amap = _amap(archive_json)
    offset = forecast_json.get("utc_offset_seconds", 0)
    today = (datetime.utcnow() + timedelta(seconds=offset)).date()
    today_str = today.isoformat()
    days = []
    for k in range(-21, 7):                       # today-21 .. today+6
        d = (today + timedelta(days=k)).isoformat()
        fm = fmap.get(d, {})
        if k < 0:                                  # completed day -> prefer observed
            am = amap.get(d)
            if am and am.get("precip") is not None:
                precip, et0, source = am["precip"], am["et0"], "observed"
            else:
                precip, et0, source = fm.get("precip"), fm.get("et0"), "recent-model"
        else:                                      # today + future -> forecast
            precip, et0, source = fm.get("precip"), fm.get("et0"), "forecast"
        days.append({"date": d, "precip": precip, "et0": et0, "source": source,
                     "tmax": fm.get("tmax"), "tmin": fm.get("tmin"), "rh": fm.get("rh"),
                     "pop": fm.get("pop"), "wind": fm.get("wind")})
    return days, today_str

def get_weather(cfg):
    fc = fetch_forecast(cfg)
    offset = fc.get("utc_offset_seconds", 0)
    today = (datetime.utcnow() + timedelta(seconds=offset)).date()
    ar = None
    try:
        ar = fetch_archive(cfg, (today - timedelta(days=21)).isoformat(), (today - timedelta(days=1)).isoformat())
    except Exception as e:
        print(f"(archive unavailable, using forecast model for past days: {e})")
    return assemble(fc, ar, cfg)

# ============================== MODEL ==============================
def build_model(days, today_str, cfg):
    idx = next((i for i, x in enumerate(days) if x["date"] == today_str), 21)
    kc, rz = cfg["kc"], cfg["rootzone"]

    et0_vals = [x["et0"] for x in days if x["et0"]]
    nz = sorted(v for v in et0_vals if v > 0)
    et0_factor = (1 / 25.4) if (nz and nz[len(nz) // 2] > 1.0) else 1.0   # mm guard

    def srain(a, b): return sum((days[i]["precip"] or 0) for i in range(max(0, a), min(len(days), b)))
    def set0(a, b):  return sum((days[i]["et0"] or 0) * et0_factor for i in range(max(0, a), min(len(days), b)))

    rain14, et14 = srain(idx - 14, idx), set0(idx - 14, idx) * kc
    net14 = rain14 - et14
    rain7, et7 = srain(idx, idx + 7), set0(idx, idx + 7) * kc
    carry = max(0.0, min(rz, net14))
    irrigate = max(0.0, round((et7 - rain7 - carry) * 20) / 20)
    sessions = 0 if irrigate <= 0 else (3 if irrigate > 0.6 else (2 if irrigate > 0.28 else 1))
    per = irrigate / sessions if sessions else 0
    minutes = round(per / cfg["sprinkler_rate_in_per_hr"] * 60) if (cfg["sprinkler_rate_in_per_hr"] > 0 and per > 0) else 0

    obs_days = sum(1 for i in range(max(0, idx - 14), idx) if days[i]["source"] == "observed")

    if net14 >= 0.3:    soil = ("Surplus", "#3E6E8E", "Soil's holding water — don't add more.")
    elif net14 >= -0.3: soil = ("Balanced", "#5C7A4E", "Moisture is about even with demand.")
    elif net14 >= -1.0: soil = ("Drawing down", "#C9772E", "Reserves dropping — keep watering deep.")
    else:               soil = ("Deficit", "#B4502B", "Dry stretch — stress risk without irrigation.")

    fc = []
    for i in range(idx, min(idx + 7, len(days))):
        x = days[i]; dt = datetime.strptime(x["date"], "%Y-%m-%d")
        tmin, tmax, rh = x["tmin"], x["tmax"], x["rh"]
        risk, rc = "Low", "#5C7A4E"
        if rh is not None and tmin is not None:
            if tmin >= 68 and rh >= 90:   risk, rc = "High", "#B4502B"
            elif tmin >= 65 and rh >= 85: risk, rc = "Mod", "#C9772E"
        lbl = dt.strftime("%#m/%#d") if sys.platform == "win32" else dt.strftime("%-m/%-d")
        fc.append(dict(date=x["date"], dow=dt.strftime("%a"), label=lbl,
                       tmax=round(tmax) if tmax is not None else None,
                       tmin=round(tmin) if tmin is not None else None,
                       rain=x["precip"] or 0, pop=x["pop"], wind=round(x["wind"] or 0),
                       rh=rh, risk=risk, rc=rc,
                       prev_rain=(days[i - 1]["precip"] or 0 if i > 0 else 0)))

    days_since = None
    if cfg["last_mow_date"]:
        days_since = (date.today() - datetime.strptime(cfg["last_mow_date"], "%Y-%m-%d").date()).days
    mow_idx, best = -1, -1e9
    for i, f in enumerate(fc):
        sc = 100 - i * 3
        if f["pop"] is not None: sc -= f["pop"] * 0.7
        if f["rain"] > 0.1: sc -= 40
        if f["prev_rain"] > 0.25: sc -= 25
        if f["tmax"] and f["tmax"] > 92: sc -= 35
        elif f["tmax"] and f["tmax"] > 88: sc -= 12
        if f["wind"] > 22: sc -= 8
        if sc > best: best, mow_idx = sc, i
    needs_mow = None if days_since is None else days_since >= 5

    return dict(fc=fc, mow_idx=mow_idx, mow=fc[mow_idx] if fc else None,
                days_since=days_since, needs_mow=needs_mow, obs_days=obs_days,
                rain14=round(rain14, 2), et14=round(et14, 2), net14=round(net14, 2),
                rain7=round(rain7, 2), et7=round(et7, 2), carry=round(carry, 2),
                irrigate=irrigate, sessions=sessions, per=round(per, 2), minutes=minutes, soil=soil)

# ============================== PRODUCT MATH (sq footage) ==============================
def product_math(products, area):
    out = []
    for p in products:
        amt = p.get("rate_amount")
        unit = p.get("rate_unit", "lb")
        per = p.get("per_sqft", 1000) or 1000
        row = {"name": p.get("name", ""), "tool": p.get("tool", "")}
        if amt is not None:
            total = amt * area / per
            per_lbl = f"{int(per/1000)}k sqft" if per % 1000 == 0 else f"{per} sqft"
            row["rate"] = f"{amt} {unit} / {per_lbl}"
            bag = p.get("bag_size")
            if bag:
                nb = math.ceil(total / bag)
                row["total"] = f"{total:.1f} {unit} → {nb} × {bag} {unit} bag" + ("s" if nb > 1 else "")
            else:
                row["total"] = f"{total:.1f} {unit} for your {int(area):,} sqft"
        else:
            row["rate"] = p.get("rate_note", "set rate_amount")
            row["total"] = ""
        out.append(row)
    return out

# ============================== CALENDAR ==============================
def calendar_view(cfg, windows):
    now = date.today(); y = now.year
    active = nxt = None
    for w in windows:
        if date(y, *w["start"]) <= now <= date(y, *w["end"]): active = w
        if now < date(y, *w["start"]) and nxt is None: nxt = w
    yr = y
    if nxt is None and windows: nxt, yr = windows[0], y + 1
    nstart = date(yr, *nxt["start"]) if nxt else now
    return active, nxt, yr, nstart - timedelta(days=cfg["lead_time_days"])

# ============================== TEXT BRIEF ==============================
def text_brief(m, cfg, windows, observations, recurring=[]):
    active, nxt, yr, buy_by = calendar_view(cfg, windows)
    L = ["=" * 54, f"  TURF CONSOLE  ·  {cfg['location_name']}",
         f"  Week of {date.today():%b %-d}  ·  generated {datetime.now():%a %-I:%M %p}", "=" * 54]
    if m["irrigate"] <= 0:
        L.append("\n[WATER]  Skip the sprinklers — rain + soil reserves cover the week.")
    else:
        ln = f"\n[WATER]  Run ~{m['irrigate']}\u2033 total"
        if m["sessions"]: ln += f", {m['sessions']} deep soak(s)"
        if m["minutes"]: ln += f" (~{m['minutes']} min each)"
        L.append(ln + ".")
    L.append(f"         Soil: {m['soil'][0]} ({'+' if m['net14']>=0 else ''}{m['net14']}\u2033 net). "
             f"Trailing balance from {m['obs_days']}/14 days observed.")
    irr_d = recommend_irrigation_days(m["fc"], m["irrigate"], m["sessions"], m["mow_idx"], cfg["sprinkler_rate_in_per_hr"])
    if irr_d:
        L.append("         Run 5\u20138 AM on:")
        for d in irr_d:
            f = d["day"]; min_s = f" ({d['minutes']} min/zone)" if d["minutes"] else ""
            rain_s = " \u2014 rain likely, check forecast" if d["rain_likely"] else ""
            L.append(f"           {f['dow']} {f['label']}: {d['amount']}\u2033{min_s}{rain_s}")
    if m["mow"]:
        L.append(f"\n[MOW]    " + ("Not yet (cut %dd ago). Revisit ~%s %s." % (m['days_since'], m['mow']['dow'], m['mow']['label'])
                 if m["needs_mow"] is False else f"Best day: {m['mow']['dow']} {m['mow']['label']}. One-third rule at {cfg['hoc_inches']}\u2033."))
    dz = next((f for f in m["fc"] if f["risk"] == "High"), None) or next((f for f in m["fc"] if f["risk"] == "Mod"), None)
    L.append(f"\n[DISEASE] {dz['risk']} brown-patch {dz['dow']} (low {dz['tmin']}\u00b0, {dz['rh']}% RH). Dawn water only, hold N."
             if dz else "\n[DISEASE] Low brown-patch pressure this week.")
    if observations:
        L.append("\n[WATCHING]")
        for o in observations[:4]:
            L.append(f"           \u2022 {o.get('note','')} (since {o.get('date','')})")
    now_wins = [(w, date(yr,*w["start"]), date(yr,*w["end"]))
                for w in windows if date(yr,*w["start"]) <= date.today() <= date(yr,*w["end"])]
    if now_wins:
        for w, wst, wen in now_wins:
            L.append(f"\n[NOW]    {w['window']} ({wst:%b %-d}\u2013{wen:%b %-d})")
            for t in w["tasks"]: L.append(f"             \u2022 {t}")
    if nxt:
        L.append(f"\n[NEXT]   {nxt['window']} ({date(yr,*nxt['start']):%b %-d}\u2013{date(yr,*nxt['end']):%b %-d}) \u2014 stock by {buy_by:%a %b %-d}:")
        for t in nxt["tasks"]: L.append(f"             \u2022 {t}")
    if recurring:
        today_r = date.today()
        due_soon, not_started_r = [], []
        for p in recurring:
            last = p.get("last_applied", ""); iv = p.get("interval_days", 28)
            if not last:
                not_started_r.append(p.get("name", ""))
            else:
                try:
                    nd = datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=iv)
                    if (nd - today_r).days <= 14:
                        due_soon.append((p.get("name",""), nd, (nd-today_r).days))
                except Exception: pass
        if not_started_r or due_soon:
            L.append("\n[RECURRING]")
            for n in not_started_r:
                L.append(f"           ! {n} \u2014 not yet started this season")
            for n, nd, dd in due_soon:
                s = "OVERDUE" if dd < 0 else f"in {dd}d"
                L.append(f"           \u203a {n} \u2014 due {nd:%b %-d} ({s})")
    L.append("\n[7-DAY]  " + "  ".join(f"{f['dow']} {f['tmax']}/{f['tmin']}\u00b0 {f['rain']:.2f}\u2033" for f in m["fc"]))
    L.append("=" * 54)
    return "\n".join(L)

# ============================== IRRIGATION + RECURRING INTELLIGENCE ==============================
def recommend_irrigation_days(fc, irrigate, sessions, mow_idx, sprinkler_rate):
    """Pick best days this week to run irrigation, spread to avoid rain and mow day."""
    if irrigate <= 0 or sessions == 0 or not fc: return []
    amount_per = round(irrigate / sessions, 2)
    minutes = round(amount_per / sprinkler_rate * 60) if sprinkler_rate > 0 else 0
    scored = []
    for i, f in enumerate(fc):
        sc = 0; pop = f["pop"] or 0; rain = f["rain"] or 0; tmax = f["tmax"] or 85
        if rain > 0.25: sc += 100      # rain will cover it, skip
        elif rain > 0.1: sc += 40
        sc += pop * 0.4                # penalise rainy-looking days
        if tmax > 95: sc += 10         # more evaporation loss on hottest days
        if i == mow_idx: sc += 15      # prefer not to soak right before mowing
        sc += i * 1.5                  # slight preference for earlier in week
        scored.append((sc, i, f))
    scored.sort(key=lambda x: x[0])
    picks = []
    if sessions == 1:
        picks = [scored[0][1]]
    elif sessions == 2:
        first = [x for x in scored if x[1] <= 3]; second = [x for x in scored if x[1] >= 3]
        if first: picks.append(first[0][1])
        for _, idx, _ in second:
            if not picks or abs(idx - picks[0]) >= 2: picks.append(idx); break
        if len(picks) < 2 and second: picks.append(second[0][1])
    else:
        for third in [[x for x in scored if x[1]<=2],[x for x in scored if 2<x[1]<=4],[x for x in scored if x[1]>4]]:
            if third: picks.append(third[0][1])
    result = []
    for idx in sorted(set(picks))[:sessions]:
        f = fc[idx]
        result.append({"day":f,"amount":amount_per,"minutes":minutes,
                       "rain_likely":((f["pop"] or 0)>=50 or (f["rain"] or 0)>=0.2)})
    return result

def build_recurring_html(recurring, fc):
    if not recurring: return ""
    today = date.today()
    max_fc_temp = max((f["tmax"] for f in fc if f["tmax"]), default=80)
    hot_days = sum(1 for f in fc if (f["tmax"] or 0) >= 90)
    max_bp = "High" if any(f["risk"]=="High" for f in fc) else ("Mod" if any(f["risk"]=="Mod" for f in fc) else "Low")
    def fc_idx(d): i=(d-today).days; return i if 0<=i<len(fc) else None
    def next_dry(from_i):
        for i in range(from_i, len(fc)):
            if (fc[i]["pop"] or 0)<40 and (fc[i]["rain"] or 0)<0.1: return fc[i]
        return None
    rows = []
    for p in recurring:
        name=p.get("name",""); tool=p.get("tool",""); rate=p.get("rate","")
        interval=p.get("interval_days",28); last=p.get("last_applied","")
        season_end=p.get("season_end",""); max_apps=p.get("max_applications")
        skip_note=p.get("skip_note","")
        upcoming=[]
        if last:
            try:
                last_d=datetime.strptime(last,"%Y-%m-%d").date()
                for k in range(1,6):
                    nd=last_d+timedelta(days=interval*k)
                    if season_end and nd>datetime.strptime(season_end,"%Y-%m-%d").date(): break
                    if max_apps and k>max_apps: break
                    upcoming.append(nd)
            except ValueError: pass
        next_due=upcoming[0] if upcoming else None
        days_until=(next_due-today).days if next_due else None
        if not last: urg="var(--amber)"; next_label="Not yet applied this season"
        elif next_due is None: urg="var(--ink-soft)"; next_label="Season complete"
        elif days_until<=0: urg="var(--terra)"; next_label=f"OVERDUE by {abs(days_until)}d"
        elif days_until<=7: urg="var(--amber)"; next_label=f"{next_due:%b %-d} \u2014 in {days_until}d"
        else: urg="var(--moss)"; next_label=f"{next_due:%b %-d} \u2014 in {days_until}d"
        intel=""
        is_fung=any(kw in name.lower() for kw in ["fungicide","propicon"])
        is_pgr=any(kw in name.lower() for kw in ["pgr","pramaxis","growth"])
        # Layer 1 — rain window: if due this week and rain forecast on that day, push to next dry
        if next_due and days_until is not None and 0<=days_until<=7:
            di=fc_idx(next_due)
            if di is not None and ((fc[di]["pop"] or 0)>=50 or (fc[di]["rain"] or 0)>=0.15):
                dry=next_dry(di+1)
                alt=(dry["dow"]+" "+dry["label"]) if dry else "next dry morning"
                intel+=f'<div style="font-size:11px;color:var(--sky);margin-top:4px">\U0001F327 Rain on due date \u2014 wait for dry leaves. Apply {alt} instead.</div>'
        # Layer 2 — fungicide auto-tighten: brown patch pressure is up, don't wait
        if is_fung and max_bp in ("High","Mod") and days_until is not None and days_until>5:
            dry=next_dry(0); ds=(dry["dow"]+" "+dry["label"]) if dry else "soonest dry morning"
            intel+=f'<div style="font-size:11px;color:var(--terra);margin-top:4px">\U0001F344 Brown patch {max_bp} \u2014 don\'t wait {days_until}d. Apply {ds} at preventative rate.</div>'
            urg="var(--terra)"; next_label=f"Apply early \u2014 was {next_due:%b %-d}"
        # Layer 3 — PGR heat extension: most of week is 90°F+, grass growth stalled
        if is_pgr and hot_days>=3 and days_until is not None and days_until<=10:
            ext=next_due+timedelta(days=interval) if next_due else None
            ext_s=ext.strftime("%b %-d") if ext else "next cycle"
            intel+=f'<div style="font-size:11px;color:var(--amber);margin-top:4px">\u26a0 {hot_days}d \u226590\u00b0F forecast \u2014 grass growth stalled. Skip this cycle; next due {ext_s}.</div>'
        elif is_pgr and max_fc_temp>=90 and not intel:
            intel+=f'<div style="font-size:11px;color:var(--amber);margin-top:4px">\u26a0 {max_fc_temp}\u00b0F forecast \u2014 reduce rate if lawn shows heat stress.</div>'
        # Generic heat note for other products (e.g. Torocity)
        if not intel and skip_note and "85" in skip_note and max_fc_temp>=85:
            dry=next_dry(0); ds=(dry["dow"]+" "+dry["label"]) if dry else "coolest morning"
            intel+=f'<div style="font-size:11px;color:var(--amber);margin-top:4px">\u26a0 {max_fc_temp}\u00b0F forecast \u2014 apply {ds} early AM only.</div>'
        following=" \u2192 ".join(d.strftime("%b %-d") for d in upcoming[1:3]) if len(upcoming)>1 else ""
        last_html=(f'<span>Last: {last}</span>' if last
                   else '<span style="color:var(--amber)">Set last_applied in lawn.yaml or log with button when you first apply.</span>')
        rows.append(
            f'<div style="padding:11px 0;border-bottom:1px dashed var(--line)">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap">'
            f'<span style="font-weight:600;color:var(--forest);font-size:14px">{name}</span>'
            f'<span style="font-family:\'Spline Sans Mono\',monospace;font-size:13px;font-weight:600;color:{urg}">{next_label}</span></div>'
            f'<div style="font-size:11px;color:var(--ink-soft);margin-top:3px">{tool} \u00b7 {rate} \u00b7 every {interval}d</div>'
            f'<div style="font-size:11px;color:var(--ink-soft);margin-top:2px">{last_html}{(" \u2192 "+following) if following else ""}</div>'
            f'{intel}</div>')
    return (f'<div class="card"><h2>\U0001F501 Recurring Applications</h2>'
            f'<div class="sub">Schedule adapts for rain windows, disease pressure, and heat. Log each application to keep dates current.</div>'
            f'{"".join(rows)}</div>')

# ============================== HTML ==============================
CSS = r"""
:root{--paper:#F1EDE0;--paper2:#F7F4EA;--card:#FBF9F1;--ink:#26231C;--ink-soft:#5A5547;--line:#D8D2BF;--forest:#1E3A29;--forest2:#2C5238;--moss:#5C7A4E;--lime:#8FA31E;--lime-bright:#A8C022;--amber:#C9772E;--terra:#B4502B;--sky:#3E6E8E}
*{box-sizing:border-box;margin:0;padding:0}body{background:var(--paper);color:var(--ink);font-family:'Spline Sans',sans-serif;background-image:radial-gradient(120% 90% at 100% 0%,rgba(143,163,30,.10),transparent 55%),radial-gradient(120% 90% at 0% 100%,rgba(30,58,41,.10),transparent 55%);min-height:100vh}
.wrap{max-width:760px;margin:0 auto;padding:22px 16px 60px}.mono{font-family:'Spline Sans Mono',monospace}
.eyebrow{font-family:'Spline Sans Mono',monospace;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--moss)}
header{border-bottom:2px solid var(--forest);padding-bottom:14px;margin-bottom:20px}
.mt{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}
h1{font-family:'Fraunces',serif;font-weight:800;font-size:clamp(34px,9vw,52px);line-height:.92;letter-spacing:-.02em;color:var(--forest)}
h1 em{font-style:italic;font-weight:500;color:var(--lime)}
.meta{text-align:right;font-size:12px;color:var(--ink-soft);line-height:1.5}.meta b{color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px;box-shadow:0 10px 30px -18px rgba(30,40,25,.55)}
.card h2{font-family:'Fraunces',serif;font-weight:600;font-size:20px;color:var(--forest);margin-bottom:2px;display:flex;align-items:center;gap:9px}
.card .sub{font-size:12.5px;color:var(--ink-soft);margin-bottom:14px}
.tag{font-family:'Spline Sans Mono',monospace;font-size:10px;letter-spacing:.12em;text-transform:uppercase;padding:2px 7px;border-radius:6px;border:1px solid var(--line);color:var(--moss);background:var(--paper2)}
.verdict{background:linear-gradient(165deg,var(--forest),var(--forest2));color:#EEF3E4;border:none}.verdict h2{color:#EEF3E4}.verdict .sub{color:#B8C9AC}
.v-row{display:flex;gap:14px;padding:13px 0;border-bottom:1px solid rgba(255,255,255,.12);align-items:flex-start}.v-row:last-child{border:none;padding-bottom:2px}
.v-icon{font-size:22px;width:26px;text-align:center}.v-label{font-family:'Spline Sans Mono',monospace;font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--lime-bright);margin-bottom:3px}
.v-headline{font-family:'Fraunces',serif;font-size:21px;font-weight:600;line-height:1.12;color:#F4F8EC}.v-detail{font-size:13px;color:#C4D3B8;margin-top:3px;line-height:1.45}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.stat{background:var(--card);padding:12px 10px;text-align:center}.stat .n{font-family:'Spline Sans Mono',monospace;font-size:23px;font-weight:600;color:var(--forest)}.stat .u{font-size:10px;color:var(--ink-soft);margin-top:5px}
.bal-track{position:relative;height:26px;background:var(--paper);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:8px 0}
.bal-mid{position:absolute;top:0;bottom:0;left:50%;width:1px;background:var(--ink-soft);opacity:.4}.bal-fill{position:absolute;top:0;bottom:0;border-radius:6px}
.bal-cap{display:flex;justify-content:space-between;font-size:11px;color:var(--ink-soft)}.pill{display:inline-block;font-family:'Spline Sans Mono',monospace;font-size:11px;font-weight:600;padding:4px 9px;border-radius:20px}
.strip{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}.day{background:var(--paper2);border:1px solid var(--line);border-radius:9px;padding:8px 4px 9px;text-align:center;position:relative}
.day.mow{border-color:var(--lime);box-shadow:0 0 0 1px var(--lime)}.day .dow{font-size:10px;color:var(--ink-soft);text-transform:uppercase}
.day .hi{font-family:'Spline Sans Mono',monospace;font-weight:600;font-size:15px;color:var(--terra)}.day .lo{font-family:'Spline Sans Mono',monospace;font-size:11px;color:var(--sky)}
.rainbar{height:30px;display:flex;align-items:flex-end;justify-content:center;margin:5px 0 3px}.rainbar i{width:7px;background:var(--sky);border-radius:2px 2px 0 0;min-height:2px;opacity:.85}
.day .rin{font-family:'Spline Sans Mono',monospace;font-size:9.5px;color:var(--sky)}.mowtag{position:absolute;top:-8px;left:50%;transform:translateX(-50%);font-size:9px;background:var(--lime);color:#1c2410;padding:1px 6px;border-radius:10px;font-family:'Spline Sans Mono',monospace;font-weight:600}
.drow{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px dashed var(--line)}.drow:last-child{border:none}
.ddot{width:10px;height:10px;border-radius:50%}.dd-date{font-family:'Spline Sans Mono',monospace;font-size:12px;width:54px;color:var(--ink-soft)}.dd-risk{font-weight:600;font-size:13px;width:74px}.dd-note{font-size:12px;color:var(--ink-soft);flex:1}
.winnow{border-left:3px solid var(--lime);padding:4px 0 4px 13px;margin-bottom:12px}.winnow .wt{font-family:'Fraunces',serif;font-size:17px;font-weight:600;color:var(--forest)}
.winnow .wd{font-family:'Spline Sans Mono',monospace;font-size:11px;color:var(--moss)}.winnow ul{margin:7px 0 0;padding-left:0;list-style:none}.winnow li{font-size:13px;padding:3px 0;color:var(--ink-soft);display:flex;gap:8px}.winnow li b{color:var(--ink);font-weight:500}
.buyby{display:inline-flex;align-items:center;gap:6px;background:#F5E4CF;color:var(--terra);border:1px solid #E6C9A6;border-radius:8px;padding:5px 10px;font-size:12px;font-weight:600;margin-top:8px}
.obs{display:flex;gap:10px;padding:8px 0;border-bottom:1px dashed var(--line);font-size:13px}.obs:last-child{border:none}.obs .od{font-family:'Spline Sans Mono',monospace;font-size:11px;color:var(--terra);width:60px;flex:0 0 auto}
.rate{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px dashed var(--line);font-size:13px}.rate:last-child{border:none}
.rate .rn{color:var(--ink)}.rate .rt{font-family:'Spline Sans Mono',monospace;font-size:11.5px;color:var(--moss);text-align:right;min-width:46%}
.rate .rt b{color:var(--forest);font-weight:600;display:block;font-size:12.5px}
footer{text-align:center;font-size:11px;color:var(--ink-soft);margin-top:24px;line-height:1.6}a{color:var(--moss)}
"""

def render_html(m, cfg, windows, prod_rows, observations, recurring=[]):
    today = date.today(); wk_end = today + timedelta(days=6); irr = m["irrigate"]
    irr_line = ("Skip the sprinklers. Rain + soil reserves cover the week." if irr <= 0 else
                f"Run about <b>{irr}\u2033</b> total" +
                (f", {m['sessions']} deep soak{'s' if m['sessions']>1 else ''}" if m['sessions'] else "") +
                (f" (~{m['minutes']} min each)" if m['minutes'] else "") + ".")
    if m["mow"]:
        mow_hl, mow_line = (("Probably not yet", f"Cut {m['days_since']}d ago. Hold; revisit ~<b>{m['mow']['dow']} {m['mow']['label']}</b>.")
                            if m["needs_mow"] is False else
                            (f"{m['mow']['dow']} {m['mow']['label']}", f"Driest, mildest day. One-third rule at {cfg['hoc_inches']}\u2033."))
    else:
        mow_hl, mow_line = "This week", "Pick the driest, coolest day."
    dz = next((f for f in m["fc"] if f["risk"] == "High"), None) or next((f for f in m["fc"] if f["risk"] == "Mod"), None)
    dz_hl = (("High" if dz["risk"] == "High" else "Moderate") + " brown-patch risk") if dz else "Clear"
    dz_line = (f"{'High' if dz['risk']=='High' else 'Moderate'} pressure {dz['dow']} (warm night, {dz['rh']}% RH). Dawn water only, hold the nitrogen."
               if dz else "Low disease pressure — nights aren't warm/humid enough.")

    net = m["net14"]; pct = max(0, min(100, 50 + net * 33))
    fill_color = "var(--sky)" if net >= 0 else "var(--amber)"
    fill_left, fill_w = (50, abs(pct - 50)) if net >= 0 else (pct, abs(pct - 50))

    max_rain = max([0.25] + [f["rain"] for f in m["fc"]]); days_html = ""
    for i, f in enumerate(m["fc"]):
        ismow = (i == m["mow_idx"] and m["needs_mow"] is not False)
        rain_str = f"{f['rain']:.2f}" if f["rain"] > 0 else "\u2014"
        pop_str = (str(f["pop"]) + "%") if f["pop"] is not None else ""
        days_html += (f"""<div class="day {'mow' if ismow else ''}">{'<span class="mowtag">MOW</span>' if ismow else ''}"""
                      f"""<div class="dow">{f['dow']}</div><div class="hi">{f['tmax']}\u00b0</div><div class="lo">{f['tmin']}\u00b0</div>"""
                      f"""<div class="rainbar"><i style="height:{max(2,round(f['rain']/max_rain*28))}px"></i></div>"""
                      f"""<div class="rin">{rain_str}</div><div class="rin" style="color:var(--ink-soft)">{pop_str}</div></div>""")

    drows = "".join(f"""<div class="drow"><span class="ddot" style="background:{f['rc']}"></span>"""
                    f"""<span class="dd-date">{f['dow']} {f['label']}</span>"""
                    f"""<span class="dd-risk" style="color:{f['rc']}">{'Moderate' if f['risk']=='Mod' else f['risk']}</span>"""
                    f"""<span class="dd-note">{'low '+str(f['tmin'])+'\u00b0, '+str(f['rh'])+'% RH' if f['rh'] is not None else '\u2014'}</span></div>"""
                    for f in m["fc"])

    obs_card = ""
    if observations:
        rows = "".join(f"""<div class="obs"><span class="od">{o.get('date','')}</span><span>{o.get('note','')}</span></div>""" for o in observations)
        obs_card = f"""<div class="card"><h2>\U0001F50D Watching</h2><div class="sub">Open issues from your log.</div>{rows}</div>"""

    # Build full-year schedule card
    sched_parts = []
    s_now = date.today(); s_y = s_now.year
    s_rows = []
    for w in windows:
        st, en = date(s_y, *w["start"]), date(s_y, *w["end"])
        status = "past" if s_now > en else ("active" if st <= s_now <= en else "upcoming")
        s_rows.append((w, status, st, en))
    def bb(st): return (st - timedelta(days=cfg["lead_time_days"])).strftime("%b %-d")
    def tasklist(w, color):
        return "".join(f'<li style="list-style:none;padding:3px 0;font-size:13px;color:var(--ink-soft)">'
                       f'<span style="color:{color};margin-right:7px">\u203a</span>{t}</li>' for t in w["tasks"])
    for w, status, st, en in s_rows:
        if status == "active":
            sched_parts.append(
                f'<div style="border-left:3px solid var(--lime-bright);padding:6px 0 6px 14px;margin-bottom:14px">'
                f'<div style="font-size:10px;font-family:\'Spline Sans Mono\',monospace;letter-spacing:.14em;'
                f'text-transform:uppercase;color:var(--lime);margin-bottom:4px">Active now</div>'
                f'<div style="font-family:\'Fraunces\',serif;font-size:18px;font-weight:600;color:var(--forest)">{w["window"]}</div>'
                f'<div style="font-size:11px;color:var(--moss);margin-bottom:8px">{st:%b %-d} \u2013 {en:%b %-d}</div>'
                f'<ul style="padding:0;margin:0">{tasklist(w,"var(--lime)")}</ul></div>')
    for i, (w, status, st, en) in enumerate([(r[0],r[1],r[2],r[3]) for r in s_rows if r[1]=="upcoming"]):
        buy_label = bb(st)
        if i == 0:
            sched_parts.append(
                f'<div style="border-left:3px solid var(--line);padding:4px 0 6px 14px;margin-bottom:10px">'
                f'<div style="font-size:10px;font-family:\'Spline Sans Mono\',monospace;color:var(--ink-soft);'
                f'margin-bottom:4px">Next up \u2014 stock by {buy_label}</div>'
                f'<div style="font-family:\'Fraunces\',serif;font-size:16px;font-weight:600;color:var(--forest)">{w["window"]}</div>'
                f'<div style="font-size:11px;color:var(--moss);margin-bottom:6px">{st:%b %-d} \u2013 {en:%b %-d}</div>'
                f'<ul style="padding:0;margin:0">{tasklist(w,"var(--moss)")}</ul>'
                f'<div class="buyby" style="margin-top:8px">\U0001F6D2 Stock up by {buy_label} '
                f'<span style="opacity:.7">({cfg["lead_time_days"]}d {cfg["retailer"]})</span></div></div>')
        else:
            sched_parts.append(
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
                f'padding:7px 0;border-top:1px dashed var(--line);font-size:13px">'
                f'<span><b style="color:var(--forest)">{w["window"]}</b> '
                f'<span style="color:var(--ink-soft);font-size:11px">{st:%b %-d}\u2013{en:%b %-d}</span></span>'
                f'<span style="font-size:11px;color:var(--moss);white-space:nowrap">stock by {buy_label}</span></div>')
    past = [w for w, status, st, en in s_rows if status == "past"]
    if past:
        names = " \u00b7 ".join(f'<span style="text-decoration:line-through;opacity:.35">{w["window"]}</span>' for w in past)
        sched_parts.append(f'<div style="font-size:11px;color:var(--ink-soft);border-top:1px dashed var(--line);'
                           f'padding-top:8px;margin-top:6px">Done this year: {names}</div>')
    sched_html = f'<div class="card"><h2>\U0001F4CB Your Schedule</h2>{"".join(sched_parts)}</div>'

    rate_rows = "".join(f"""<div class="rate"><span class="rn">{r['name']}<br><span style="font-size:11px;color:var(--moss)">{r['tool']}</span></span>"""
                        f"""<span class="rt">{('<b>'+r['total']+'</b>') if r['total'] else ''}{r['rate']}</span></div>""" for r in prod_rows)
    rate_card = (f"""<div class="card"><h2>\U0001F9EA How Much to Apply <span class="tag">{int(cfg['turf_area_sqft']):,} sqft</span></h2>"""
                 f"""<div class="sub">Rate × your yard = total to put down (and bags to buy). Edit rates in lawn.yaml.</div>{rate_rows}</div>""") if prod_rows else ""

    recurring_card = build_recurring_html(recurring, m["fc"])
    irr_days = recommend_irrigation_days(m["fc"], m["irrigate"], m["sessions"], m["mow_idx"], cfg["sprinkler_rate_in_per_hr"])
    if irr_days:
        irr_rows = ""
        for d in irr_days:
            fd = d["day"]
            min_s = f" \u00b7 {d['minutes']} min/zone" if d["minutes"] else " \u00b7 set sprinkler_rate_in_per_hr for minutes"
            rain_s = ' <span style="font-size:10px;color:var(--sky)">(rain likely \u2014 check before running)</span>' if d["rain_likely"] else ""
            irr_rows += (f'<div style="display:flex;justify-content:space-between;align-items:center;'
                        f'padding:8px 0;border-bottom:1px dashed var(--line)">'
                        f'<span style="font-weight:600;color:var(--forest)">{fd["dow"]} {fd["label"]}</span>'
                        f'<span class="mono" style="font-size:13px;color:var(--sky)">{d["amount"]}\u2033{min_s}</span>'
                        f'</div>{rain_s}')
        irr_card = (f'<div class="card"><h2>\U0001F4A7 When to Irrigate</h2>'
                   f'<div class="sub">Best days this week, picked around rain and your mow day. '
                   f'Always 5\u20138 AM \u2014 never evening (brown patch). Deep and infrequent beats daily light watering.</div>'
                   f'{irr_rows}'
                   f'<div style="font-size:11px;color:var(--ink-soft);margin-top:10px">'
                   f'Cycle-soak: 15 min on, wait 30 min, 15 min again \u2014 reduces runoff on compacted soil.</div></div>')
    else:
        irr_card = ""

    soil = m["soil"]
    body = f"""
    <header><div class="eyebrow">Weekly Field Report · {cfg['location_name'].split(',')[0]}</div>
    <div class="mt"><h1>Turf<br><em>Console</em></h1>
    <div class="meta"><b>{cfg['location_name']}</b><br>Week of {today:%b %-d} \u2013 {wk_end:%b %-d}<br>
    <span class="mono" style="font-size:11px">generated {datetime.now():%a %-I:%M %p}</span></div></div></header>

    <div class="card verdict"><h2>This Week's Call</h2><div class="sub">The three decisions that matter, up top.</div>
    <div class="v-row"><div class="v-icon">\U0001F4A7</div><div><div class="v-label">Irrigation</div>
    <div class="v-headline">{'No watering needed' if irr<=0 else str(irr)+'\u2033 this week'}</div><div class="v-detail">{irr_line}</div></div></div>
    <div class="v-row"><div class="v-icon">\U0001F69C</div><div><div class="v-label">Mowing</div>
    <div class="v-headline">{mow_hl}</div><div class="v-detail">{mow_line}</div></div></div>
    <div class="v-row"><div class="v-icon">\U0001F344</div><div><div class="v-label">Disease Watch</div>
    <div class="v-headline">{dz_hl}</div><div class="v-detail">{dz_line}</div></div></div></div>

    <div class="card"><h2>\U0001F4A7 Water Balance <span class="tag">{m['obs_days']}/14 days observed</span></h2>
    <div class="sub">Soil: <span class="pill" style="background:{soil[1]}22;color:{soil[1]}">{soil[0]}</span> — {soil[2]}</div>
    <div class="stats"><div class="stat"><div class="n">{m['rain14']}\u2033</div><div class="u">rain fell (14d)</div></div>
    <div class="stat"><div class="n">{m['et14']}\u2033</div><div class="u">turf used (14d)</div></div>
    <div class="stat"><div class="n">{'+' if net>=0 else ''}{net}\u2033</div><div class="u">net balance</div></div></div>
    <div class="bal-track"><div class="bal-mid"></div><div class="bal-fill" style="left:{fill_left}%;width:{fill_w}%;background:{fill_color}"></div></div>
    <div class="bal-cap"><span>\u2190 drier</span><span>even</span><span>wetter \u2192</span></div>
    <div class="sub" style="margin:12px 0 0">Coming 7 days: turf wants <b>{m['et7']}\u2033</b>, rain may bring <b>{m['rain7']}\u2033</b>, soil banks <b>{m['carry']}\u2033</b>. Make up: <b>{irr}\u2033</b>.</div></div>

    {irr_card}

    <div class="card"><h2>\U0001F324 7-Day Outlook</h2><div class="sub">Highs / lows · rain inches & chance · mow day flagged.</div><div class="strip">{days_html}</div></div>

    {obs_card}

    <div class="card"><h2>\U0001F344 Brown-Patch Pressure</h2><div class="sub">Warm nights (>65\u00b0) + humid air. Your #1 fescue threat in a KC summer.</div>
    {drows}<div class="sub" style="margin-top:12px">When risk climbs: water before dawn, never evening, ease off nitrogen, bag clippings if you spot circular tan patches.</div></div>

    {sched_html}

    {recurring_card}

    {rate_card}

    <footer>Past days = observed (Open-Meteo / ERA5); future = forecast. Program from lawn.yaml.<br>
    Irrigation = (Kc \u00d7 reference ET) \u2212 rainfall \u2212 banked soil moisture, sized in inches of depth.<br>
    <span class="mono" style="font-size:10px">Turf Console · edit lawn.yaml to change your plan</span></footer>
    """
    return (f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">"""
            f"""<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Turf Console</title>"""
            f"""<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>"""
            f"""<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,600;0,9..144,800;1,9..144,500&family=Spline+Sans:wght@400;500;600&family=Spline+Sans+Mono:wght@400;500;600&display=swap" rel="stylesheet">"""
            f"""<style>{CSS}</style></head><body><div class="wrap">{body}</div></body></html>""")

# ============================== EMAIL ==============================
def send_email(subject, body_text, html_path, ec):
    sender = os.environ.get("TURF_SMTP_USER") or ec["sender"]
    recipient = os.environ.get("TURF_SMTP_TO") or ec["recipient"]
    msg = EmailMessage(); msg["Subject"], msg["From"], msg["To"] = subject, sender, recipient
    msg.set_content(body_text)
    try:
        msg.add_attachment(Path(html_path).read_text(encoding="utf-8").encode("utf-8"),
                           maintype="text", subtype="html", filename="turf-console.html")
    except Exception: pass
    pwd = os.environ.get(ec["password_env"]) or ec.get("password", "")
    if not pwd or "@" not in sender:
        print("EMAIL skipped: set TURF_SMTP_PASS / TURF_SMTP_USER / TURF_SMTP_TO."); return
    with smtplib.SMTP(ec["smtp_host"], ec["smtp_port"], timeout=30) as srv:
        srv.starttls(context=ssl.create_default_context()); srv.login(sender, pwd); srv.send_message(msg)
    print(f"EMAIL sent to {recipient}.")

# ============================== MAIN ==============================
def main():
    cfg, windows, products, observations, _, recurring = load_program()
    try:
        days, today_str = get_weather(cfg)
    except Exception as e:
        print(f"ERROR fetching weather: {e}"); sys.exit(1)
    m = build_model(days, today_str, cfg)
    prod_rows = product_math(products, cfg["turf_area_sqft"])
    brief = text_brief(m, cfg, windows, observations, recurring)
    print(brief)
    HTML_FILE.write_text(render_html(m, cfg, windows, prod_rows, observations, recurring), encoding="utf-8")
    print(f"\nDashboard written to {HTML_FILE}")
    if "--email" in sys.argv:
        try: send_email(f"Turf Console \u2014 week of {date.today():%b %-d}", brief, HTML_FILE, EMAIL)
        except Exception as e: print(f"EMAIL failed: {e}")
    if "--open" in sys.argv:
        webbrowser.open(HTML_FILE.as_uri())

if __name__ == "__main__":
    main()
