#!/bin/zsh
# Nattlig kjøring (launchd 23:30): Garmin -> Lyftr + lokal cache, deretter Lyftr -> Obsidian.
set -u
cd "$HOME/lyftr" || exit 1
echo "=== $(date '+%F %T') ==="
if [ -d "$HOME/.garminconnect" ]; then
  if grep -qE '^LYFTR_PASSWORD=.+' "$HOME/.config/lyftr/env" 2>/dev/null; then
    ./.venv/bin/python scripts/garmin_import.py </dev/null || echo "garmin_import feilet (fortsetter med Obsidian-synk)"
  else
    ./.venv/bin/python scripts/garmin_import.py --no-lyftr </dev/null || echo "garmin_import (cache) feilet"
    echo "LYFTR_PASSWORD mangler i ~/.config/lyftr/env – vekt ble ikke sendt til Lyftr"
  fi
else
  echo "Ingen Garmin-tokens (~/.garminconnect) – hopper over Garmin. Kjør: scripts/garmin_import.py --login"
fi
/usr/bin/python3 scripts/obsidian_sync.py
