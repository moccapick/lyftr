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
`Obsidian/Trening/{Økter,Vekt,Kosthold}`. Kjøres av launchd hver kveld 23:30 og ved innlogging:
`~/Library/LaunchAgents/no.danieldahl.lyftr-obsidian-sync.plist`, logg i `logs/obsidian-sync.log`.
Manuell kjøring: `python3 scripts/obsidian_sync.py [--dry-run]`.
Tekst under `<!-- lyftr:slutt -->` i en note bevares ved neste synk.
