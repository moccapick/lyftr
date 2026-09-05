# Lokalt oppsett (Daniel)

Fork av [Cawlumm/lyftr](https://github.com/Cawlumm/lyftr). `upstream`-remote peker på originalen:
`git fetch upstream && git merge upstream/main` henter nye versjoner.

## Kjøring
```bash
colima start                      # Docker-daemon (colima)
cd ~/lyftr && docker compose up -d --build
```
- Lokalt: http://localhost:8080
- Tailscale: https://lyftr.tailb952f7.ts.net (krever Tailscale på enheten)
- Data: `./data/lyftr.db` (SQLite, WAL). Backup = kopier filen når stacken er stoppet.
- Konfig: `.env` (ikke i git). `REGISTRATION=first-user` stenger registrering etter første bruker.

## Tailscale-login (første gang)
Containeren kjører `tailscale up` uten tidsavbrudd og skriver én login-URL i loggen:
```bash
docker compose logs tailscale | grep "login.tailscale.com"
```
Åpne den og godkjenn. Alternativ: lag en auth-key på
https://login.tailscale.com/admin/settings/keys og legg den i `TS_AUTHKEY` i `.env`.
State ligger i `tailscale-state/`, så login overlever restart og oppdatering.

Engangsoppsett i tailnettet: «Serve» må være aktivert (containeren skriver lenken i loggen
hvis den mangler). HTTPS-sertifikat hentes automatisk. `CORS_ORIGIN` i `.env` må inneholde
https-adressen; etter endring: `docker compose up -d backend`.

Tailscale må være installert og innlogget på telefon/Mac som skal nå appen.

## Obsidian-speil
`scripts/obsidian_sync.py` leser SQLite-filen og skriver markdown til
det private vaultet `Dropbox-Privat/Privat/Obsidian Privat/Trening/{Økter,Vekt,Kosthold}` (ikke jobb-vaultet). Kjøres av launchd hver kveld 23:30 og ved innlogging:
`~/Library/LaunchAgents/no.danieldahl.lyftr-obsidian-sync.plist`, logg i `logs/obsidian-sync.log`.
Manuell kjøring: `python3 scripts/obsidian_sync.py [--dry-run]`.
Tekst under `<!-- lyftr:slutt -->` i en note bevares ved neste synk.

## Programimport
Skriv programmet som markdown (se `Trening/Programmer/Eksempel - PPL.md` i det private vaultet) og kjør:
```bash
python3 ~/lyftr/scripts/program_import.py "<fil>" --dry-run   # slår opp øvelser, oppretter ikke
python3 ~/lyftr/scripts/program_import.py "<fil>"             # oppretter programmet
```
Format: `# Programnavn`, `## Dag`, `- Øvelse: 4x8 @ 80 | pause 120 | notat: …`, `hvile` for hviledag.
Passord hentes fra `LYFTR_PASSWORD` eller prompt. Øvelsesnavn er engelske (open-exercise-db).

## Garmin → Lyftr (kroppsvekt) og → Obsidian (kroppssammensetning)
`scripts/garmin_import.py` (kjøres med `.venv/bin/python`, biblioteket `garminconnect`, uoffisielt API).
1. Engangs: `~/lyftr/.venv/bin/python ~/lyftr/scripts/garmin_import.py --login` (spør om Garmin e-post/passord/MFA, tokens i `~/.garminconnect`).
2. Legg Lyftr-passordet i `~/.config/lyftr/env` (`LYFTR_PASSWORD=…`, filen er chmod 600).
3. Historikk: `… garmin_import.py --all --dry-run`, deretter `… garmin_import.py --all`.
Vekt går inn i Lyftr med Garmins tidsstempel (dedupe på minutt og dag+vekt). Fett %, muskelmasse,
beinmasse, vann og BMI lagres i `data/garmin_body.json` og vises i Obsidian-notatene under `Vekt/`.
Nattlig: `scripts/nightly.sh` (launchd 23:30) kjører Garmin-import og deretter Obsidian-synk. Logg: `logs/nightly.log`.
Feilsøking: `--debug` lagrer rå Garmin-respons i `data/`.

## Strava-webhook (Garmin-økt → puls/kalorier i Obsidian-notatet)
Container `strava` (`integrations/strava/server.py`, port 8090) med egen Tailscale-node `lyftr-hook`
(`tailscale-hook`-containeren) og Funnel på 443: https://lyftr-hook.tailb952f7.ts.net. Strava godtar ikke egen
port i callback-URL, og lyftr-noden (UI) skal forbli tailnet-only, derfor to noder.
1. Garmin Connect → Innstillinger → Tilkoblede apper → Strava: slå på automatisk opplasting.
2. Aktiver Funnel i tailnettet (lenken står i `docker compose logs tailscale` hvis den mangler).
3. Opprett API-app på https://www.strava.com/settings/api med Authorization Callback Domain
   `lyftr-hook.tailb952f7.ts.net`. Legg `STRAVA_CLIENT_ID` og `STRAVA_CLIENT_SECRET` i `.env`, `docker compose up -d strava`.
4. Åpne https://lyftr-hook.tailb952f7.ts.net/auth og godkjenn (tokens → `data/strava_tokens.json`).
5. `docker compose exec strava python server.py subscribe` (Strava validerer `/webhook` med verify-token).
Hendelser skrives til `data/strava_activities.json`; launchd-agenten `no.danieldahl.lyftr-strava-watch`
kjører Obsidian-synk på filendring. Aktiviteten matches mot Lyftr-økt startet innen 3 timer, ellers får den
egen note `… (Garmin).md`. Test uten Strava: sett `STRAVA_FIXTURE_DIR=/app/fixtures` i `.env` og POST en
hendelse med `object_id` 1234567890 til http://127.0.0.1:8090/webhook.
