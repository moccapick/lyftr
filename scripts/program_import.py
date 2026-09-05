#!/usr/bin/env python3
"""
Importerer et treningsprogram til Lyftr fra en markdown-fil.

Format (én fil = ett program):

    # PPL 6 dager
    Notat: 3 uker progresjon, deload uke 4      (valgfri linje)

    ## Push A
    - Barbell Bench Press: 4x8 @ 80 | pause 150 | notat: pause i bunn
    - Incline Dumbbell Press: 3x10 @ 30
    - Cable Fly: 3x12
    - Triceps Pushdown: 8,10,12 @ 35            (ulike reps per sett)

    ## Hvile
    hvile

    ## Pull A
    - Pull-Up: 4x6
    ...

Sett-syntaks:  SETTxREPS [@ VEKT]   eller   REPS,REPS,REPS [@ VEKT]  eller  REPS,REPS @ V1,V2
Vekt i samme enhet som brukeren har valgt i Lyftr (kg). Pause i sekunder (standard 90).
Øvelsesnavn slås opp i Lyftrs øvelsesbibliotek (engelske navn, open-exercise-db).

Bruk:
  program_import.py Trening/Programmer/PPL.md --parse-only     # bare vis tolkningen
  program_import.py Trening/Programmer/PPL.md --dry-run        # logg inn, slå opp øvelser, ikke opprett
  program_import.py Trening/Programmer/PPL.md                  # opprett programmet

Innlogging: --email (standard LYFTR_EMAIL) og passord fra LYFTR_PASSWORD eller prompt.
Server: --base-url (standard LYFTR_URL eller http://localhost:8080).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_URL = os.environ.get("LYFTR_URL", "http://localhost:8080")
DEFAULT_EMAIL = os.environ.get("LYFTR_EMAIL", "danielchr.dahl@gmail.com")
DEFAULT_REST = 90


@dataclass
class SetSpec:
    reps: int
    weight: float = 0.0


@dataclass
class ExerciseSpec:
    name: str
    sets: list[SetSpec]
    rest: int = DEFAULT_REST
    notes: str = ""
    line: int = 0
    exercise_id: int | None = None
    resolved_name: str = ""


@dataclass
class DaySpec:
    name: str
    rest_day: bool = False
    exercises: list[ExerciseSpec] = field(default_factory=list)


@dataclass
class ProgramSpec:
    name: str
    notes: str = ""
    days: list[DaySpec] = field(default_factory=list)


# ---------- parsing ----------

def parse_number(s: str) -> float:
    return float(s.strip().replace(",", "."))


def parse_sets(spec: str, line: int) -> list[SetSpec]:
    spec = spec.strip()
    weight_part = ""
    if "@" in spec:
        spec, weight_part = (p.strip() for p in spec.split("@", 1))
        weight_part = re.sub(r"\s*kg\s*$", "", weight_part, flags=re.I)

    m = re.fullmatch(r"(\d+)\s*[xX×]\s*(\d+)", spec)
    if m:
        reps = [int(m.group(2))] * int(m.group(1))
    elif re.fullmatch(r"\d+(\s*,\s*\d+)*", spec):
        reps = [int(r) for r in re.split(r"\s*,\s*", spec)]
    else:
        raise ValueError(f"linje {line}: forstår ikke sett-spesifikasjonen «{spec}» (bruk 4x8 eller 8,8,6)")

    if not weight_part:
        weights = [0.0] * len(reps)
    else:
        parts = [p for p in re.split(r"\s*[,;/]\s*", weight_part) if p]
        try:
            ws = [parse_number(p) for p in parts]
        except ValueError:
            raise ValueError(f"linje {line}: ugyldig vekt «{weight_part}»")
        if len(ws) == 1:
            weights = ws * len(reps)
        elif len(ws) == len(reps):
            weights = ws
        else:
            raise ValueError(f"linje {line}: {len(ws)} vekter for {len(reps)} sett")
    return [SetSpec(r, w) for r, w in zip(reps, weights)]


def parse_exercise_line(text: str, line: int) -> ExerciseSpec:
    body = text.lstrip("-*• ").strip()
    if ":" not in body:
        raise ValueError(f"linje {line}: mangler «:» mellom øvelse og sett i «{body}»")
    name, rest_of = body.split(":", 1)
    segments = [s.strip() for s in rest_of.split("|")]
    sets = parse_sets(segments[0], line)
    ex = ExerciseSpec(name=name.strip(), sets=sets, line=line)
    for seg in segments[1:]:
        low = seg.lower()
        m = re.fullmatch(r"(pause|rest|hvile)\s*[:=]?\s*(\d+)\s*(s|sek)?", low)
        if m:
            ex.rest = int(m.group(2))
            continue
        m = re.match(r"(notat|note|kommentar)\s*[:=]\s*(.*)", seg, flags=re.I)
        if m:
            ex.notes = m.group(2).strip()
            continue
        raise ValueError(f"linje {line}: ukjent felt «{seg}» (bruk «pause 120» eller «notat: …»)")
    return ex


def parse_program(path: Path) -> ProgramSpec:
    prog: ProgramSpec | None = None
    day: DaySpec | None = None
    in_frontmatter = False
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.rstrip()
        if n == 1 and line.strip() == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
            continue
        s = line.strip()
        if not s or s.startswith("<!--") or s.startswith("%%"):
            continue
        if s.startswith("# "):
            if prog:
                raise ValueError(f"linje {n}: bare én «# Programnavn» per fil")
            prog = ProgramSpec(name=s[2:].strip())
            continue
        if prog is None:
            raise ValueError(f"linje {n}: filen må starte med «# Programnavn»")
        if s.startswith("## "):
            day = DaySpec(name=s[3:].strip())
            prog.days.append(day)
            continue
        m = re.match(r"(notat|note)\s*:\s*(.*)", s, flags=re.I)
        if m and day is None:
            prog.notes = m.group(2).strip()
            continue
        if day is None:
            raise ValueError(f"linje {n}: «{s}» står før første «## Dag»")
        if s.lower() in ("hvile", "hviledag", "rest", "rest day"):
            day.rest_day = True
            continue
        if s.startswith(("-", "*", "•")):
            day.exercises.append(parse_exercise_line(s, n))
            continue
        raise ValueError(f"linje {n}: forstår ikke «{s}»")
    if prog is None:
        raise ValueError("fant ingen «# Programnavn»")
    for d in prog.days:
        if not d.rest_day and not d.exercises:
            raise ValueError(f"dagen «{d.name}» har ingen øvelser (skriv «hvile» hvis det er hviledag)")
    return prog


def describe(prog: ProgramSpec) -> str:
    out = [f"Program: {prog.name}" + (f"  ({prog.notes})" if prog.notes else "")]
    for i, d in enumerate(prog.days, 1):
        if d.rest_day:
            out.append(f"  Dag {i}: {d.name} – hvile")
            continue
        out.append(f"  Dag {i}: {d.name}")
        for ex in d.exercises:
            sets = " / ".join(f"{s.reps}×{s.weight:g}" if s.weight else f"{s.reps}" for s in ex.sets)
            tail = f"  → {ex.resolved_name} (#{ex.exercise_id})" if ex.exercise_id else ""
            out.append(f"    - {ex.name}: {sets}  pause {ex.rest}s{tail}")
    return "\n".join(out)


# ---------- API ----------

class Api:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.token = ""

    def call(self, method: str, path: str, body=None, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            raise SystemExit(f"{method} {path} → HTTP {e.code}: {msg}")
        except urllib.error.URLError as e:
            raise SystemExit(f"Får ikke kontakt med {url}: {e.reason}")

    def login(self, email: str, password: str):
        res = self.call("POST", "/auth/login", {"email": email, "password": password})
        self.token = res["data"]["token"]

    def search_exercises(self, q: str) -> list[dict]:
        res = self.call("GET", "/exercises", params={"q": q, "limit": 25})
        return res.get("data") or []


def resolve_exercises(api: Api, prog: ProgramSpec, interactive: bool):
    cache: dict[str, dict] = {}
    for d in prog.days:
        for ex in d.exercises:
            key = ex.name.lower()
            if key in cache:
                hit = cache[key]
            else:
                results = api.search_exercises(ex.name)
                if not results:
                    raise SystemExit(f"linje {ex.line}: fant ingen øvelse som matcher «{ex.name}»")
                exact = [r for r in results if r["name"].lower() == key]
                hit = exact[0] if exact else None
                if hit is None:
                    if interactive and len(results) > 1:
                        print(f"\n«{ex.name}» – flere treff:")
                        for i, r in enumerate(results[:10], 1):
                            print(f"  {i}. {r['name']} ({r.get('muscle_group','')}, {r.get('equipment','')})")
                        choice = input("Velg nummer [1]: ").strip() or "1"
                        hit = results[int(choice) - 1]
                    else:
                        hit = results[0]
                cache[key] = hit
            ex.exercise_id = hit["id"]
            ex.resolved_name = hit["name"]


def build_payload(prog: ProgramSpec) -> dict:
    return {
        "name": prog.name,
        "notes": prog.notes,
        "days": [
            {
                "is_rest_day": d.rest_day,
                "name": d.name,
                "exercises": [] if d.rest_day else [
                    {
                        "exercise_id": ex.exercise_id,
                        "notes": ex.notes,
                        "rest_seconds": ex.rest,
                        "sets": [
                            {"set_number": i, "target_reps": s.reps, "target_weight": s.weight}
                            for i, s in enumerate(ex.sets, 1)
                        ],
                    }
                    for ex in d.exercises
                ],
            }
            for d in prog.days
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--base-url", default=DEFAULT_URL)
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--parse-only", action="store_true", help="Vis tolkningen uten å kontakte serveren")
    ap.add_argument("--dry-run", action="store_true", help="Slå opp øvelser, men ikke opprett programmet")
    ap.add_argument("--no-interactive", action="store_true", help="Ta første treff uten å spørre")
    ap.add_argument("--json", action="store_true", help="Skriv API-payload som JSON")
    args = ap.parse_args()

    try:
        prog = parse_program(args.file)
    except ValueError as e:
        sys.exit(f"Feil i {args.file}: {e}")

    if args.parse_only:
        print(describe(prog))
        return

    password = os.environ.get("LYFTR_PASSWORD") or getpass.getpass(f"Passord for {args.email}: ")
    api = Api(args.base_url)
    api.login(args.email, password)
    resolve_exercises(api, prog, interactive=not args.no_interactive and sys.stdin.isatty())
    print(describe(prog))
    payload = build_payload(prog)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("\n[dry-run] programmet ble ikke opprettet.")
        return
    res = api.call("POST", "/programs", payload)
    created = res.get("data", {})
    print(f"\nOpprettet program «{created.get('name', prog.name)}» (id {created.get('id', '?')}) "
          f"med {len(prog.days)} dager.")


if __name__ == "__main__":
    main()
