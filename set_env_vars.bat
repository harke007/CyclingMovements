@echo off
REM ============================================================
REM set_env_vars.bat
REM
REM Zet de environment variables die CyclingMovements.py gebruikt,
REM zodat je tokens/secrets niet hardcoded in het script hoeven te
REM staan. Vul de waardes hieronder in en dubbelklik dit bestand
REM (of run het vanuit een terminal).
REM
REM Gebruikt "setx" -> dit zet de variabele PERMANENT op user-niveau
REM (net als handmatig instellen via Windows System Properties).
REM Na het draaien van dit script moet je een NIEUW terminal-
REM venster / Spyder / Python-sessie openen voordat os.environ.get(...)
REM de nieuwe waarde ziet - een al openstaand venster leest 'm niet
REM automatisch opnieuw in.
REM ============================================================

setx TREDICT_TOKEN "vul-hier-je-tredict-token-in"

setx STRAVA_CLIENT_ID "vul-hier-je-strava-client-id-in"
setx STRAVA_CLIENT_SECRET "vul-hier-je-strava-client-secret-in"
setx STRAVA_REFRESH_TOKEN "vul-hier-je-strava-refresh-token-in"

echo.
echo Environment variables gezet. Open een NIEUW terminal-/Python-
echo venster om ze te kunnen gebruiken.
pause
