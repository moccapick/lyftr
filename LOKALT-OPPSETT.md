# Lokalt oppsett (Daniel)

Fork av [Cawlumm/lyftr](https://github.com/Cawlumm/lyftr). `upstream`-remote peker på originalen:
`git fetch upstream && git merge upstream/main` henter nye versjoner.

## Kjøring
```bash
colima start                      # Docker-daemon (colima)
cd ~/lyftr && docker compose up -d --build
```
- Lokalt: http://localhost:8080
- Tailscale: https://lyftr.<tailnet>.ts.net (etter login, se under)
- Data: `./data/lyftr.db` (SQLite, WAL). Backup = kopier filen når stacken er stoppet.
- Konfig: `.env` (ikke i git). `REGISTRATION=first-user` stenger registrering etter første bruker.

## Tailscale-login (første gang)
Containeren restarter hvert minutt til den er logget inn, og login-URL-en byttes hver gang:
```bash
docker compose logs -f tailscale | grep "login.tailscale.com"
```
Åpne siste URL og godkjenn. Alternativ: lag en auth-key på
https://login.tailscale.com/admin/settings/keys og legg den i `TS_AUTHKEY` i `.env`.
Etter login: sett `CORS_ORIGIN=http://localhost:8080,https://lyftr.<tailnet>.ts.net` i `.env`
og kjør `docker compose up -d backend`.

Tailscale må også være installert på telefon/Mac som skal nå appen. HTTPS-sertifikat ordnes
automatisk av `tailscale/serve.json` (krever at «HTTPS Certificates» er slått på i tailnettet:
https://login.tailscale.com/admin/dns).

## Obsidian-speil
`scripts/obsidian_sync.py` leser SQLite-filen og skriver markdown til
`Obsidian/Trening/{Økter,Vekt,Kosthold}`. Kjøres av launchd hver kveld 23:30 og ved innlogging:
`~/Library/LaunchAgents/no.danieldahl.lyftr-obsidian-sync.plist`, logg i `logs/obsidian-sync.log`.
Manuell kjøring: `python3 scripts/obsidian_sync.py [--dry-run]`.
Tekst under `<!-- lyftr:slutt -->` i en note bevares ved neste synk.
