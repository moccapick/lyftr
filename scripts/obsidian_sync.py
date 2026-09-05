#!/usr/bin/env python3
"""
Speiler Lyftr-databasen (SQLite) til Obsidian som markdown-notater.

Én vei: Lyftr er primærlager, Obsidian er lesbart speil. Skriptet eier alt
over markøren `<!-- lyftr:slutt -->` i hver note; tekst under markøren bevares.

Struktur i vaultet (standard: <vault>/Trening):
  Økter/YYYY-MM-DD <navn>.md   én note per økt, sett-tabell per øvelse, PR-merking
  Vekt/YYYY-MM-DD.md           én note per dag med vektlogg
  Kosthold/YYYY-MM-DD.md       én note per dag med kalorier/makroer og måltider

Bruk:
  obsidian_sync.py                      # standardstier
  obsidian_sync.py --db X --vault-dir Y # egne stier
  obsidian_sync.py --dry-run            # vis hva som ville blitt skrevet
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
DEFAULT_DB = HOME / "lyftr" / "data" / "lyftr.db"
DEFAULT_VAULT_DIR = (
    HOME / "Library/CloudStorage/Dropbox-Privat/Privat/Obsidian Privat/Trening"
)
TZ = ZoneInfo("Europe/Oslo")
MARKER = "<!-- lyftr:slutt -->"
GARMIN_BODY = Path(os.environ.get("LYFTR_GARMIN_BODY", HOME / "lyftr" / "data" / "garmin_body.json"))  # skrives av garmin_import.py
LBS_TO_KG = 0.45359237

MANEDER = [
    "januar", "februar", "mars", "april", "mai", "juni", "juli",
    "august", "september", "oktober", "november", "desember",
]
MALTID = {"breakfast": "Frokost", "lunch": "Lunsj", "dinner": "Middag", "snacks": "Mellommåltid"}


# ---------- hjelpere ----------

def parse_dt(value) -> dt.datetime | None:
    """Tolerant parser for SQLite DATETIME-strenger. Naive tider tolkes som UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        d = value
    else:
        s = str(value).strip().replace(" ", "T", 1)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # klipp nanosekunder til mikrosekunder
        m = re.match(r"^(.*?\.\d{6})\d+(.*)$", s)
        if m:
            s = m.group(1) + m.group(2)
        # "+0200" -> "+02:00"
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
        try:
            d = dt.datetime.fromisoformat(s)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(TZ)


def norsk_dato(d: dt.date) -> str:
    return f"{d.day}. {MANEDER[d.month - 1]} {d.year}"


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', "", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:80] or "Økt"


def fmt_num(x: float, decimals: int = 1) -> str:
    if x is None:
        return ""
    if abs(x - round(x)) < 1e-9:
        return f"{int(round(x))}"
    return f"{x:.{decimals}f}".rstrip("0").rstrip(".")


def fmt_kg(x: float) -> str:
    return f"{fmt_num(x, 1)} kg"


def yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def yaml_list(items) -> str:
    return "[" + ", ".join(yaml_str(str(i)) for i in items) + "]"


def e1rm(weight: float, reps: int) -> float:
    """Epley."""
    if reps <= 0 or weight <= 0:
        return 0.0
    if reps == 1:
        return weight
    return weight * (1 + reps / 30.0)


# ---------- database ----------

def snapshot_db(src: Path) -> sqlite3.Connection:
    """Tar en konsistent kopi (håndterer WAL) og åpner kopien read-only."""
    tmp = Path(tempfile.mkdtemp(prefix="lyftr-sync-")) / "lyftr.db"
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    dst_conn = sqlite3.connect(tmp)
    with dst_conn:
        src_conn.backup(dst_conn)
    src_conn.close()
    dst_conn.row_factory = sqlite3.Row
    return dst_conn


def columns(conn, table) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def pick_user(conn, email: str | None):
    if email:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            sys.exit(f"Fant ingen bruker med e-post {email}")
        return row
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        print(f"Flere brukere i databasen, bruker den første ({rows[0]['email']}). "
              f"Velg med --user-email.", file=sys.stderr)
    return rows[0]


# ---------- innhenting ----------

def load_workouts(conn, user_id: int, to_kg: float):
    wcols = columns(conn, "workouts")
    has_day = "program_day_id" in wcols
    sql = """
        SELECT w.*, p.name AS program_name
               {day_sel}
        FROM workouts w
        LEFT JOIN programs p ON p.id = w.program_id
        {day_join}
        WHERE w.user_id = ?
        ORDER BY w.started_at, w.id
    """.format(
        day_sel=", pd.name AS program_day_name" if has_day else ", NULL AS program_day_name",
        day_join="LEFT JOIN program_days pd ON pd.id = w.program_day_id" if has_day else "",
    )
    workouts = []
    for w in conn.execute(sql, (user_id,)):
        exercises = []
        ex_rows = conn.execute(
            """SELECT we.id, we.notes, we.order_index, we.rest_seconds,
                      e.name, e.muscle_group, e.equipment, e.category
               FROM workout_exercises we JOIN exercises e ON e.id = we.exercise_id
               WHERE we.workout_id = ? ORDER BY we.order_index, we.id""",
            (w["id"],),
        ).fetchall()
        for ex in ex_rows:
            sets = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM sets WHERE workout_exercise_id = ? ORDER BY set_number, id",
                    (ex["id"],),
                )
            ]
            for s in sets:
                s["weight_kg"] = (s["weight"] or 0) * to_kg
            exercises.append({**dict(ex), "sets": sets})
        workouts.append({**dict(w), "exercises": exercises, "started": parse_dt(w["started_at"])})
    return workouts


def mark_prs(workouts):
    """PR = beste vekt eller beste e1RM for øvelsen er høyere enn i alle tidligere økter."""
    best_w: dict[str, float] = defaultdict(float)
    best_e: dict[str, float] = defaultdict(float)
    for w in workouts:  # kronologisk
        w["prs"] = []
        for ex in w["exercises"]:
            work = [s for s in ex["sets"] if not s["is_warmup"] and s["reps"] > 0 and s["weight_kg"] > 0]
            if not work:
                ex["best"] = None
                ex["is_pr"] = False
                continue
            top = max(work, key=lambda s: (s["weight_kg"], s["reps"]))
            top_e = max(e1rm(s["weight_kg"], s["reps"]) for s in work)
            ex["best"] = (top["weight_kg"], top["reps"], top_e)
            name = ex["name"]
            is_pr = top["weight_kg"] > best_w[name] + 1e-9 or top_e > best_e[name] + 1e-9
            ex["is_pr"] = is_pr and (best_w[name] > 0 or best_e[name] > 0 or True)
            if is_pr:
                w["prs"].append(name)
            best_w[name] = max(best_w[name], top["weight_kg"])
            best_e[name] = max(best_e[name], top_e)


# ---------- rendering ----------

def render_workout(w, unit_label: str) -> tuple[str, str]:
    started = w["started"] or parse_dt(w["created_at"]) or dt.datetime.now(TZ)
    date = started.date()
    name = (w["name"] or "Økt").strip()
    filename = f"{date.isoformat()} {safe_filename(name)}.md"

    total_sets = sum(len([s for s in ex["sets"] if not s["is_warmup"]]) for ex in w["exercises"])
    volume = sum(
        (s["reps"] or 0) * s["weight_kg"]
        for ex in w["exercises"] for s in ex["sets"] if not s["is_warmup"]
    )
    muscles = sorted({ex["muscle_group"] for ex in w["exercises"] if ex["muscle_group"]})
    duration_min = round((w["duration"] or 0) / 60)

    fm = [
        "---",
        "type: trening-okt",
        f"lyftr_id: {w['id']}",
        f"dato: {date.isoformat()}",
        f"start: {started.strftime('%Y-%m-%dT%H:%M')}",
        f"navn: {yaml_str(name)}",
        f"program: {yaml_str(w['program_name']) if w['program_name'] else 'null'}",
        f"programdag: {yaml_str(w['program_day_name']) if w['program_day_name'] else 'null'}",
        f"varighet_min: {duration_min}",
        f"antall_ovelser: {len(w['exercises'])}",
        f"antall_sett: {total_sets}",
        f"volum_kg: {round(volume)}",
        f"ovelser: {yaml_list(ex['name'] for ex in w['exercises'])}",
        f"muskelgrupper: {yaml_list(muscles)}",
        f"pr: {yaml_list(w['prs'])}",
        "lyftr_sync: true",
        "---",
    ]

    body = [f"# {name} – {norsk_dato(date)}", ""]
    meta = [f"**Start:** {started.strftime('%H:%M')}"]
    if duration_min:
        meta.append(f"**Varighet:** {duration_min} min")
    meta.append(f"**Volum:** {fmt_num(volume, 0)} kg")
    meta.append(f"**Sett:** {total_sets}")
    if w["program_name"]:
        prog = w["program_name"] + (f" / {w['program_day_name']}" if w["program_day_name"] else "")
        meta.append(f"**Program:** {prog}")
    body.append(" · ".join(meta))
    if w["prs"]:
        body.append("")
        body.append("🏆 **PR:** " + ", ".join(w["prs"]))
    if w["notes"]:
        body += ["", "> " + w["notes"].strip().replace("\n", "\n> ")]

    for ex in w["exercises"]:
        tags = ", ".join(t for t in (ex["muscle_group"], ex["equipment"]) if t)
        body += ["", f"## {ex['name']}" + (f" ({tags})" if tags else "")]
        if ex["notes"]:
            body.append(f"_{ex['notes'].strip()}_")
        if ex["sets"]:
            timed = any(s["duration"] for s in ex["sets"])
            dist = any(s["distance"] for s in ex["sets"])
            cols = ["Sett", "Reps", "Vekt"]
            if timed:
                cols.append("Tid")
            if dist:
                cols.append("Distanse")
            cols += ["RPE", ""]
            body.append("| " + " | ".join(cols) + " |")
            body.append("|" + "---|" * len(cols))
            for s in ex["sets"]:
                row = [
                    str(s["set_number"]),
                    fmt_num(s["reps"]) if s["reps"] else "",
                    fmt_kg(s["weight_kg"]) if s["weight_kg"] else "",
                ]
                if timed:
                    row.append(f"{s['duration']} s" if s["duration"] else "")
                if dist:
                    row.append(f"{fmt_num(s['distance'])} m" if s["distance"] else "")
                row.append(fmt_num(s["rpe"]) if s["rpe"] else "")
                row.append("oppvarming" if s["is_warmup"] else "")
                body.append("| " + " | ".join(row) + " |")
        if ex.get("best"):
            bw, br, be = ex["best"]
            line = f"Beste: {fmt_kg(bw)} × {br} (e1RM {fmt_num(be, 0)} kg)"
            if ex["is_pr"]:
                line += " 🏆 PR"
            body.append("")
            body.append(line)

    if unit_label != "kg":
        body += ["", f"_Vekter er regnet om fra {unit_label} til kg._"]

    return filename, "\n".join(fm + [""] + body)


def load_garmin_body() -> dict[str, list[dict]]:
    """Garmin-målinger gruppert per dato (YYYY-MM-DD), sortert på tid."""
    if not GARMIN_BODY.exists():
        return {}
    try:
        data = json.loads(GARMIN_BODY.read_text())
    except (OSError, ValueError):
        return {}
    by_day: dict[str, list[dict]] = defaultdict(list)
    for rec in data.values():
        if rec.get("date"):
            by_day[rec["date"]].append(rec)
    for recs in by_day.values():
        recs.sort(key=lambda r: r.get("ts", ""))
    return by_day


def render_weight(date: dt.date, logs, to_kg: float, garmin: list[dict] | None = None) -> str:
    last = logs[-1]
    g = garmin[-1] if garmin else None
    fm = [
        "---",
        "type: vekt",
        f"dato: {date.isoformat()}",
        f"vekt_kg: {fmt_num(last['weight'] * to_kg, 1)}",
        f"antall_malinger: {len(logs)}",
    ]
    if g:
        for key, label in (("fat_pct", "fett_pct"), ("muscle_kg", "muskelmasse_kg"), ("bone_kg", "beinmasse_kg"),
                           ("water_pct", "vann_pct"), ("bmi", "bmi"), ("visceral_fat", "visceralt_fett"),
                           ("metabolic_age", "metabolsk_alder")):
            if g.get(key) is not None:
                fm.append(f"{label}: {g[key]}")
        fm.append("kilde: Garmin")
    fm += ["lyftr_sync: true", "---"]
    body = [f"# Vekt {norsk_dato(date)}", "", f"**{fmt_kg(last['weight'] * to_kg)}**"]
    if g:
        parts = []
        if g.get("fat_pct") is not None:
            parts.append(f"Fett {fmt_num(g['fat_pct'])} %")
        if g.get("muscle_kg") is not None:
            parts.append(f"Muskelmasse {fmt_kg(g['muscle_kg'])}")
        if g.get("bone_kg") is not None:
            parts.append(f"Beinmasse {fmt_kg(g['bone_kg'])}")
        if g.get("water_pct") is not None:
            parts.append(f"Vann {fmt_num(g['water_pct'])} %")
        if g.get("bmi") is not None:
            parts.append(f"BMI {fmt_num(g['bmi'])}")
        if parts:
            body.append(" · ".join(parts))
    if len(logs) > 1 or any(l["notes"] for l in logs):
        body += ["", "| Tid | Vekt | Notat |", "|---|---|---|"]
        for l in logs:
            t = parse_dt(l["logged_at"])
            body.append(f"| {t.strftime('%H:%M') if t else ''} | {fmt_kg(l['weight'] * to_kg)} | {l['notes'] or ''} |")
    elif last["notes"]:
        body += ["", "> " + last["notes"]]
    return "\n".join(fm + [""] + body)


def render_food(date: dt.date, logs, targets) -> str:
    tot = {k: sum((l[k] or 0) * 1 for l in logs) for k in ("calories", "protein", "carbs", "fat", "fiber")}
    fm = [
        "---",
        "type: kosthold",
        f"dato: {date.isoformat()}",
        f"kalorier: {round(tot['calories'])}",
        f"protein_g: {round(tot['protein'])}",
        f"karbo_g: {round(tot['carbs'])}",
        f"fett_g: {round(tot['fat'])}",
        f"fiber_g: {round(tot['fiber'])}",
        f"kalorimal: {targets['calorie_target']}",
        f"proteinmal: {targets['protein_target']}",
        f"antall_oppforinger: {len(logs)}",
        "lyftr_sync: true",
        "---",
    ]
    body = [
        f"# Kosthold {norsk_dato(date)}",
        "",
        f"**{round(tot['calories'])} kcal** (mål {targets['calorie_target']}) · "
        f"P {round(tot['protein'])} g · K {round(tot['carbs'])} g · F {round(tot['fat'])} g · Fiber {round(tot['fiber'])} g",
    ]
    by_meal = defaultdict(list)
    for l in logs:
        by_meal[l["meal"] or "snacks"].append(l)
    for key in ("breakfast", "lunch", "dinner", "snacks"):
        if key not in by_meal:
            continue
        items = by_meal[key]
        body += ["", f"## {MALTID.get(key, key)}", "", "| Mat | Porsjon | kcal | P | K | F |", "|---|---|---|---|---|---|"]
        for l in items:
            navn = l["name"] + (f" ({l['brand']})" if l.get("brand") else "")
            porsjon = f"{fmt_num(l['servings'])} × {l['serving_size']}".strip(" ×") if l["serving_size"] else fmt_num(l["servings"])
            body.append(
                f"| {navn} | {porsjon} | {round(l['calories'] or 0)} | {round(l['protein'] or 0)} | "
                f"{round(l['carbs'] or 0)} | {round(l['fat'] or 0)} |"
            )
    return "\n".join(fm + [""] + body)


# ---------- skriving ----------

class Writer:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.written = 0
        self.unchanged = 0

    def write(self, path: Path, generated: str):
        tail = ""
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if MARKER in existing:
                tail = existing.split(MARKER, 1)[1]
        content = generated.rstrip("\n") + "\n\n" + MARKER + (tail if tail else "\n")
        if path.exists() and path.read_text(encoding="utf-8") == content:
            self.unchanged += 1
            return
        self.written += 1
        if self.dry_run:
            print(f"[dry-run] ville skrevet {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=Path(os.environ.get("LYFTR_DB", DEFAULT_DB)))
    ap.add_argument("--vault-dir", type=Path, default=Path(os.environ.get("LYFTR_VAULT_DIR", DEFAULT_VAULT_DIR)),
                    help="Mappen i vaultet som speilet skrives til")
    ap.add_argument("--user-email", default=os.environ.get("LYFTR_USER_EMAIL"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"Finner ikke databasen: {args.db} (kjører Lyftr?)")

    conn = snapshot_db(args.db)
    user = pick_user(conn, args.user_email)
    if user is None:
        print("Ingen brukere i Lyftr ennå – ingenting å speile.")
        return
    uid = user["id"]

    settings = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (uid,)).fetchone()
    unit = (settings["weight_unit"] if settings else "lbs") or "lbs"
    to_kg = 1.0 if unit == "kg" else LBS_TO_KG
    targets = {
        "calorie_target": settings["calorie_target"] if settings else 0,
        "protein_target": settings["protein_target"] if settings else 0,
    }

    out = args.vault_dir
    wr = Writer(args.dry_run)

    # Økter
    workouts = load_workouts(conn, uid, to_kg)
    mark_prs(workouts)
    seen = set()
    for w in workouts:
        filename, content = render_workout(w, unit)
        # to økter samme dag med samme navn -> suffiks med id
        if filename in seen:
            filename = filename[:-3] + f" ({w['id']}).md"
        seen.add(filename)
        wr.write(out / "Økter" / filename, content)

    # Vekt
    wl_cols = columns(conn, "weight_logs")
    by_day = defaultdict(list)
    for r in conn.execute("SELECT * FROM weight_logs WHERE user_id = ? ORDER BY logged_at, id", (uid,)):
        d = None
        if "logged_on" in wl_cols and r["logged_on"]:
            try:
                d = dt.date.fromisoformat(r["logged_on"][:10])
            except ValueError:
                d = None
        if d is None:
            t = parse_dt(r["logged_at"])
            d = t.date() if t else None
        if d:
            by_day[d].append(r)
    garmin_by_day = load_garmin_body()
    for d, logs in sorted(by_day.items()):
        wr.write(out / "Vekt" / f"{d.isoformat()}.md",
                 render_weight(d, logs, to_kg, garmin_by_day.get(d.isoformat())))

    # Kosthold
    fl_cols = columns(conn, "food_logs")
    food_by_day = defaultdict(list)
    for r in conn.execute("SELECT * FROM food_logs WHERE user_id = ? ORDER BY logged_at, id", (uid,)):
        row = dict(r)
        d = None
        if "logged_on" in fl_cols and row.get("logged_on"):
            try:
                d = dt.date.fromisoformat(row["logged_on"][:10])
            except ValueError:
                d = None
        if d is None:
            t = parse_dt(row["logged_at"])
            d = t.date() if t else None
        if d:
            food_by_day[d].append(row)
    for d, logs in sorted(food_by_day.items()):
        wr.write(out / "Kosthold" / f"{d.isoformat()}.md", render_food(d, logs, targets))

    print(
        f"Lyftr → Obsidian ({user['email']}, enhet {unit}): "
        f"{len(workouts)} økter, {len(by_day)} vektdager, {len(food_by_day)} kostholdsdager. "
        f"{wr.written} filer skrevet, {wr.unchanged} uendret."
        + (" [dry-run]" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()
