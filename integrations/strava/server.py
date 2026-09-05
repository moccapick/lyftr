#!/usr/bin/env python3
"""
Strava-webhook-mottaker for Lyftr.

Kjede: Garmin-klokke -> Garmin Connect -> Strava (auto-synk) -> webhook hit ->
henter aktiviteten fra Strava-API-et -> skriver til data/strava_activities.json.
obsidian_sync.py på Mac-en leser filen og legger puls/kalorier inn i økt-notatene.

Endepunkter (port 8090):
  GET  /health
  GET  /webhook           Stravas abonnementsvalidering (hub.challenge)
  POST /webhook           hendelser fra Strava (svarer 200 med én gang, prosesserer i bakgrunnen)
  GET  /auth              sender deg til Strava for å godkjenne appen (engangs)
  GET  /auth/callback     mottar koden, bytter til tokens -> data/strava_tokens.json

CLI:
  python server.py serve
  python server.py subscribe        oppretter webhook-abonnement hos Strava (krever at /webhook er offentlig)
  python server.py subscriptions    viser eksisterende abonnement
  python server.py unsubscribe ID

Miljø: STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_VERIFY_TOKEN, STRAVA_PUBLIC_URL
       (f.eks. https://lyftr.tailb952f7.ts.net:8443), DATA_DIR (/app/data),
       STRAVA_FIXTURE_DIR (kun test: aktiviteter leses fra <dir>/<id>.json i stedet for Strava).
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
VERIFY_TOKEN = os.environ.get("STRAVA_VERIFY_TOKEN", "")
PUBLIC_URL = os.environ.get("STRAVA_PUBLIC_URL", "").rstrip("/")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
FIXTURE_DIR = os.environ.get("STRAVA_FIXTURE_DIR", "")
PORT = int(os.environ.get("PORT", "8090"))

TOKENS_FILE = DATA_DIR / "strava_tokens.json"
ACTIVITIES_FILE = DATA_DIR / "strava_activities.json"
EVENTS_LOG = DATA_DIR / "strava_events.log"
STRAVA_API = "https://www.strava.com/api/v3"
STRAVA_OAUTH = "https://www.strava.com/oauth"

_lock = threading.Lock()
_queue: queue.Queue = queue.Queue()


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with EVENTS_LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------- Strava HTTP ----------

def http(method: str, url: str, data: dict | None = None, token: str | None = None, form: bool = False):
    body = None
    headers = {}
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")}


def load_tokens() -> dict:
    if TOKENS_FILE.exists():
        return json.loads(TOKENS_FILE.read_text())
    return {}


def save_tokens(t: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(t, indent=1))
    try:
        TOKENS_FILE.chmod(0o600)
    except OSError:
        pass


def access_token() -> str:
    t = load_tokens()
    if not t.get("refresh_token"):
        raise RuntimeError("Ingen Strava-tokens. Åpne /auth i nettleseren først.")
    if t.get("expires_at", 0) > time.time() + 60:
        return t["access_token"]
    status, res = http("POST", f"{STRAVA_OAUTH}/token", {
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
    }, form=True)
    if status != 200:
        raise RuntimeError(f"Token-refresh feilet: {status} {res}")
    t.update({k: res[k] for k in ("access_token", "refresh_token", "expires_at")})
    save_tokens(t)
    return t["access_token"]


def fetch_activity(activity_id: int) -> dict | None:
    if FIXTURE_DIR:
        p = Path(FIXTURE_DIR) / f"{activity_id}.json"
        if p.exists():
            log(f"fixture: leser aktivitet {activity_id} fra {p}")
            return json.loads(p.read_text())
        log(f"fixture: ingen fil for {activity_id}")
        return None
    status, res = http("GET", f"{STRAVA_API}/activities/{activity_id}", token=access_token())
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"Henting av aktivitet {activity_id} feilet: {status} {res}")
    return res


# ---------- prosessering ----------

def summarize(a: dict) -> dict:
    return {
        "strava_id": a.get("id"),
        "name": a.get("name", ""),
        "type": a.get("sport_type") or a.get("type") or "",
        "start_utc": a.get("start_date", ""),
        "start_local": a.get("start_date_local", ""),
        "timezone": a.get("timezone", ""),
        "elapsed_s": a.get("elapsed_time") or 0,
        "moving_s": a.get("moving_time") or 0,
        "avg_hr": a.get("average_heartrate"),
        "max_hr": a.get("max_heartrate"),
        "calories": a.get("calories"),
        "distance_m": a.get("distance") or 0,
        "device": a.get("device_name", ""),
        "description": a.get("description") or "",
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def upsert_activity(summary: dict):
    with _lock:
        data = json.loads(ACTIVITIES_FILE.read_text()) if ACTIVITIES_FILE.exists() else {}
        data[str(summary["strava_id"])] = summary
        tmp = ACTIVITIES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        tmp.replace(ACTIVITIES_FILE)


def delete_activity(activity_id: int):
    with _lock:
        if not ACTIVITIES_FILE.exists():
            return
        data = json.loads(ACTIVITIES_FILE.read_text())
        if data.pop(str(activity_id), None) is not None:
            tmp = ACTIVITIES_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            tmp.replace(ACTIVITIES_FILE)


def process_event(ev: dict):
    if ev.get("object_type") != "activity":
        return
    aid = int(ev.get("object_id", 0))
    aspect = ev.get("aspect_type")
    if aspect == "delete":
        delete_activity(aid)
        log(f"aktivitet {aid} slettet")
        return
    for attempt in range(3):
        try:
            a = fetch_activity(aid)
            break
        except Exception as e:
            log(f"aktivitet {aid}: feil ved henting ({e}), forsøk {attempt + 1}/3")
            time.sleep(10 * (attempt + 1))
    else:
        return
    if a is None:
        log(f"aktivitet {aid} finnes ikke hos Strava")
        return
    s = summarize(a)
    upsert_activity(s)
    log(f"aktivitet {aid} lagret: {s['type']} «{s['name']}» {s['start_utc']} "
        f"{s['elapsed_s'] // 60} min, puls {s['avg_hr']}/{s['max_hr']}, {s['calories']} kcal")


def worker():
    while True:
        ev = _queue.get()
        try:
            process_event(ev)
        except Exception as e:
            log(f"feil i prosessering: {e}")
        finally:
            _queue.task_done()


# ---------- HTTP-server ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "lyftr-strava/1.0"

    def log_message(self, fmt, *args):  # roligere logg
        pass

    def _send(self, status: int, body, content_type="application/json"):
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/health":
            return self._send(200, {"ok": True, "tokens": TOKENS_FILE.exists(), "queue": _queue.qsize()})
        if url.path == "/webhook":
            if q.get("hub.mode", [""])[0] == "subscribe" and q.get("hub.verify_token", [""])[0] == VERIFY_TOKEN and VERIFY_TOKEN:
                log("abonnementsvalidering OK")
                return self._send(200, {"hub.challenge": q.get("hub.challenge", [""])[0]})
            log("abonnementsvalidering avvist (feil verify_token)")
            return self._send(403, {"error": "verify_token mismatch"})
        if url.path == "/auth":
            if not CLIENT_ID or not PUBLIC_URL:
                return self._send(500, "STRAVA_CLIENT_ID / STRAVA_PUBLIC_URL mangler i .env", "text/plain")
            params = urllib.parse.urlencode({
                "client_id": CLIENT_ID, "response_type": "code",
                "redirect_uri": f"{PUBLIC_URL}/auth/callback",
                "approval_prompt": "auto", "scope": "activity:read_all",
            })
            self.send_response(302)
            self.send_header("Location", f"{STRAVA_OAUTH}/authorize?{params}")
            self.end_headers()
            return
        if url.path == "/auth/callback":
            code = q.get("code", [""])[0]
            if not code:
                return self._send(400, f"Strava avviste: {q.get('error', [''])[0]}", "text/plain")
            status, res = http("POST", f"{STRAVA_OAUTH}/token", {
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                "code": code, "grant_type": "authorization_code",
            }, form=True)
            if status != 200:
                return self._send(500, f"Token-bytte feilet: {res}", "text/plain")
            save_tokens({k: res[k] for k in ("access_token", "refresh_token", "expires_at")}
                        | {"athlete_id": (res.get("athlete") or {}).get("id")})
            log("Strava-tokens lagret")
            return self._send(200, "Strava er koblet til Lyftr. Du kan lukke fanen.", "text/plain")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/webhook":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            return self._send(413, {"error": "too large"})
        try:
            ev = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        log(f"hendelse: {ev.get('object_type')} {ev.get('aspect_type')} id={ev.get('object_id')}")
        _queue.put(ev)
        return self._send(200, {"received": True})   # Strava krever svar innen 2 s


# ---------- CLI ----------

def cmd_subscribe():
    if not all((CLIENT_ID, CLIENT_SECRET, VERIFY_TOKEN, PUBLIC_URL)):
        sys.exit("Sett STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_VERIFY_TOKEN og STRAVA_PUBLIC_URL i .env")
    status, res = http("POST", f"{STRAVA_API}/push_subscriptions", {
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "callback_url": f"{PUBLIC_URL}/webhook", "verify_token": VERIFY_TOKEN,
    }, form=True)
    print(status, json.dumps(res, indent=1))


def cmd_subscriptions():
    status, res = http("GET", f"{STRAVA_API}/push_subscriptions?" + urllib.parse.urlencode(
        {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}))
    print(status, json.dumps(res, indent=1))


def cmd_unsubscribe(sub_id: str):
    status, res = http("DELETE", f"{STRAVA_API}/push_subscriptions/{sub_id}?" + urllib.parse.urlencode(
        {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}))
    print(status, res)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "subscribe":
        return cmd_subscribe()
    if cmd == "subscriptions":
        return cmd_subscriptions()
    if cmd == "unsubscribe":
        return cmd_unsubscribe(sys.argv[2])
    threading.Thread(target=worker, daemon=True).start()
    log(f"lytter på :{PORT} (fixture={'ja' if FIXTURE_DIR else 'nei'}, tokens={'ja' if TOKENS_FILE.exists() else 'nei'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
