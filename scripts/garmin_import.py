#!/usr/bin/env python3
"""
Henter kroppsvekt og kroppssammensetning fra Garmin Connect, legger vekten inn i
Lyftr (med riktig tidsstempel) og lagrer alle målinger lokalt slik at Obsidian-
speilet kan vise fettprosent, muskelmasse osv.

Garmin har ikke noe offisielt API for privatpersoner. Dette bruker biblioteket
`garminconnect` (uoffisielt) som logger inn som deg. Tokens lagres i ~/.garminconnect.

Første gang (interaktivt, spør om e-post/passord/MFA):
  ~/lyftr/.venv/bin/python ~/lyftr/scripts/garmin_import.py --login

Full historikk inn i Lyftr:
  ~/lyftr/.venv/bin/python ~/lyftr/scripts/garmin_import.py --all

Deretter (nattlig, henter bare nytt):
  ~/lyftr/.venv/bin/python ~/lyftr/scripts/garmin_import.py

Lyftr-innlogging: LYFTR_EMAIL / LYFTR_PASSWORD (fra miljø eller ~/.config/lyftr/env), ellers prompt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("LYFTR_DATA_DIR", HOME / "lyftr" / "data"))
BODY_CACHE = DATA_DIR / "garmin_body.json"      # leses av obsidian_sync.py
STATE_FILE = DATA_DIR / "garmin_state.json"
TOKENSTORE = os.environ.get("GARMINTOKENS", str(HOME / ".garminconnect"))
ENV_FILE = HOME / ".config" / "lyftr" / "env"
CHUNK_DAYS = 90
EARLIEST = "2010-01-01"


def load_env_file():
    """KEY=VALUE-linjer fra ~/.config/lyftr/env (chmod 600) inn i os.environ hvis ikke satt."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------- Garmin ----------

def garmin_client(interactive: bool):
    try:
        from garminconnect import Garmin
    except ImportError:
        sys.exit("garminconnect mangler. Kjør: ~/lyftr/.venv/bin/pip install garminconnect")

    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    tokens_exist = Path(TOKENSTORE).expanduser().exists()

    if not tokens_exist:
        if not interactive:
            sys.exit("Ingen Garmin-tokens. Kjør først: garmin_import.py --login")
        email = email or input("Garmin e-post: ").strip()
        password = password or getpass.getpass("Garmin passord: ")

    api = Garmin(
        email=email,
        password=password,
        prompt_mfa=(lambda: input("Garmin MFA-kode: ").strip()) if interactive else None,
    )
    api.login(TOKENSTORE)
    return api


def to_kg(v):
    """Garmin oppgir masse i gram."""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v / 1000.0, 2) if v > 500 else round(v, 2)


def pct(v):
    if v is None:
        return None
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def normalize(entry: dict) -> dict | None:
    """Ett Garmin-vektobjekt -> vår flate post. Returnerer None uten vekt."""
    weight = to_kg(entry.get("weight"))
    if not weight:
        return None
    ts_ms = entry.get("timestampGMT") or entry.get("date")
    cal = entry.get("calendarDate")
    if ts_ms:
        ts = dt.datetime.fromtimestamp(int(ts_ms) / 1000, tz=dt.timezone.utc)
    elif cal:
        ts = dt.datetime.fromisoformat(cal).replace(hour=7, tzinfo=dt.timezone.utc)
    else:
        return None
    if not cal:
        cal = ts.date().isoformat()
    return {
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": cal,
        "weight_kg": weight,
        "fat_pct": pct(entry.get("bodyFat")),
        "muscle_kg": to_kg(entry.get("muscleMass")),
        "bone_kg": to_kg(entry.get("boneMass")),
        "water_pct": pct(entry.get("bodyWater")),
        "bmi": pct(entry.get("bmi")),
        "visceral_fat": entry.get("visceralFat"),
        "metabolic_age": entry.get("metabolicAge"),
        "source": entry.get("sourceType") or "",
        "garmin_pk": entry.get("samplePk"),
    }


def extract_entries(payload) -> list[dict]:
    """Håndterer begge Garmin-endepunktene (dateRange og range) defensivt."""
    out = []
    if not isinstance(payload, dict):
        return out
    for e in payload.get("dateWeightList") or []:
        out.append(e)
    for day in payload.get("dailyWeightSummaries") or []:
        metrics = day.get("allWeightMetrics") or ([day["latestWeight"]] if day.get("latestWeight") else [])
        out.extend(m for m in metrics if m)
    return out


def fetch_garmin(api, start: dt.date, end: dt.date, debug: bool) -> list[dict]:
    records = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + dt.timedelta(days=CHUNK_DAYS - 1), end)
        payload = None
        for attempt in range(3):
            try:
                payload = api.get_body_composition(cur.isoformat(), chunk_end.isoformat())
                break
            except Exception as e:  # rate limit / nettverk
                wait = 5 * (attempt + 1)
                print(f"  {cur}–{chunk_end}: feil ({e}); prøver igjen om {wait}s", file=sys.stderr)
                time.sleep(wait)
        if payload is None:
            sys.exit(f"Ga opp på {cur}–{chunk_end}")
        if debug:
            (DATA_DIR / f"garmin_raw_{cur}.json").write_text(json.dumps(payload, indent=2))
        entries = extract_entries(payload)
        n = 0
        for e in entries:
            r = normalize(e)
            if r:
                records.append(r)
                n += 1
        print(f"  {cur} – {chunk_end}: {n} målinger")
        cur = chunk_end + dt.timedelta(days=1)
        time.sleep(1.0)
    return records


# ---------- lokal cache ----------

def load_cache() -> dict:
    if BODY_CACHE.exists():
        return json.loads(BODY_CACHE.read_text())
    return {}


def save_cache(cache: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BODY_CACHE.write_text(json.dumps(dict(sorted(cache.items())), indent=1, ensure_ascii=False))


# ---------- Lyftr ----------

class Lyftr:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.token = ""

    def call(self, method, path, body=None, params=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Lyftr {method} {path} → HTTP {e.code}: {e.read().decode(errors='replace')}")
        except urllib.error.URLError as e:
            raise SystemExit(f"Får ikke kontakt med Lyftr på {url}: {e.reason}")

    def login(self, email, password):
        self.token = self.call("POST", "/auth/login", {"email": email, "password": password})["data"]["token"]

    def all_weights(self) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self.call("GET", "/weight", params={"limit": 1000, "offset": offset}).get("data") or []
            out.extend(page)
            if len(page) < 1000:
                return out
            offset += 1000

    def add_weight(self, rec: dict):
        return self.call("POST", "/weight", {
            "weight": rec["weight_kg"],
            "notes": "Garmin",
            "logged_at": rec["ts"],
            "logged_on": rec["date"],
        })


def minute_key(ts_iso: str) -> str:
    s = ts_iso.replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return ts_iso[:16]
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")


def push_to_lyftr(records: list[dict], base_url: str, dry_run: bool) -> tuple[int, int]:
    email = os.environ.get("LYFTR_EMAIL") or input("Lyftr e-post: ").strip()
    password = os.environ.get("LYFTR_PASSWORD") or getpass.getpass(f"Lyftr passord for {email}: ")
    api = Lyftr(base_url)
    api.login(email, password)
    existing = api.all_weights()
    seen_minutes = {minute_key(w["logged_at"]) for w in existing if w.get("logged_at")}
    seen_day_weight = {(w.get("logged_on") or w["logged_at"][:10], round(float(w["weight"]), 1)) for w in existing}
    added = skipped = 0
    for rec in sorted(records, key=lambda r: r["ts"]):
        if minute_key(rec["ts"]) in seen_minutes or (rec["date"], round(rec["weight_kg"], 1)) in seen_day_weight:
            skipped += 1
            continue
        if dry_run:
            print(f"  [dry-run] ville lagt inn {rec['date']} {rec['weight_kg']} kg")
        else:
            api.add_weight(rec)
        seen_minutes.add(minute_key(rec["ts"]))
        seen_day_weight.add((rec["date"], round(rec["weight_kg"], 1)))
        added += 1
    return added, skipped


# ---------- main ----------

def main():
    load_env_file()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--login", action="store_true", help="Interaktiv Garmin-innlogging (lagrer tokens) og avslutt")
    ap.add_argument("--all", action="store_true", help=f"Hent hele historikken fra {EARLIEST}")
    ap.add_argument("--since", help="Hent fra dato YYYY-MM-DD")
    ap.add_argument("--no-lyftr", action="store_true", help="Bare oppdater lokal cache, ikke Lyftr")
    ap.add_argument("--dry-run", action="store_true", help="Ikke skriv til Lyftr")
    ap.add_argument("--base-url", default=os.environ.get("LYFTR_URL", "http://localhost:8080"))
    ap.add_argument("--fixture", type=Path, help="Les Garmin-respons fra JSON-fil i stedet for API (test)")
    ap.add_argument("--debug", action="store_true", help="Lagre rå Garmin-respons i data/")
    args = ap.parse_args()

    if args.login:
        garmin_client(interactive=True)
        print(f"Innlogget. Tokens lagret i {TOKENSTORE}.")
        return

    today = dt.date.today()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if args.fixture:
        start = today
    elif args.all:
        start = dt.date.fromisoformat(EARLIEST)
    elif args.since:
        start = dt.date.fromisoformat(args.since)
    elif state.get("last_date"):
        start = dt.date.fromisoformat(state["last_date"]) - dt.timedelta(days=7)
    else:
        sys.exit("Første kjøring: oppgi --all (hele historikken) eller --since YYYY-MM-DD")

    if args.fixture:
        payload = json.loads(args.fixture.read_text())
        records = [r for r in (normalize(e) for e in extract_entries(payload)) if r]
        print(f"Fixture: {len(records)} målinger")
    else:
        api = garmin_client(interactive=sys.stdin.isatty())
        print(f"Henter Garmin-veiinger {start} – {today} …")
        records = fetch_garmin(api, start, today, args.debug)

    cache = load_cache()
    new_in_cache = 0
    for r in records:
        if r["ts"] not in cache:
            new_in_cache += 1
        cache[r["ts"]] = r
    save_cache(cache)
    print(f"Lokal cache: {len(cache)} målinger totalt, {new_in_cache} nye → {BODY_CACHE}")

    if not args.no_lyftr and records:
        added, skipped = push_to_lyftr(records, args.base_url, args.dry_run)
        print(f"Lyftr: {added} lagt inn, {skipped} fantes fra før" + (" [dry-run]" if args.dry_run else ""))

    if not args.fixture and not args.dry_run:
        STATE_FILE.write_text(json.dumps({"last_date": today.isoformat(), "last_run": dt.datetime.now().isoformat()}))


if __name__ == "__main__":
    main()
