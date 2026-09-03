import os
import re
import csv
import gzip
import glob
import time
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import numpy as np
import requests
from PIL import Image
import geopandas as gpd
import pandas as pd

# Forceer de non-interactieve Agg-backend VOORDAT pyplot wordt
# geïmporteerd. Zonder dit kan matplotlib een GUI-backend (TkAgg/
# QtAgg) pakken waarvan de canvas-pixelgrootte mede wordt bepaald
# door de Windows-schermschaling (devicePixelRatio). Dat geeft
# inconsistente, niet-proportionele output-resoluties zodra je de
# dpi verhoogt. Met Agg is de canvas-grootte altijd exact
# figsize * dpi, ongeacht schermschaling.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import contextily as ctx
import imageio.v2 as imageio

from fitparse import FitFile
from fit2gpx import Converter
import gpxpy
import gpxpy.gpx


class CyclingMovementRenderer:
    """
    Rendert een animatie van routes (LineStrings) bovenop een basemap,
    door elk frame als JPG weg te schrijven en die daarna met ffmpeg
    samen te voegen tot een video.

    Standaard is dit gericht op fietsactiviteiten, maar via
    activity_filter="running" (bij het aanmaken van de renderer)
    wordt de hele klasse omgezet naar hardloopactiviteiten:
    Tredict-download, FIT->GPX-conversie, de officiële Strava API en
    de Strava bulk-export-CSV volgen allemaal deze ene instelling -
    inclusief welke gpx-map standaard wordt gebruikt
    ("Activities_gpx_cycling" / "Activities_gpx_running"), die
    automatisch wordt aangemaakt als hij nog niet bestaat.

    data_source verwacht een map met .gpx bestanden (geen los
    geojson/geopackage-bestand meer); vul deze map met de sync_*
    methoden hieronder, of handmatig.

    Optioneel kan deze klasse ook zelf de bron-data verzamelen:
      1) download_tredict_activities() haalt .fit/.tcx bestanden
         op van Tredict (zie tredict_call.py).
      2) convert_fit_folder_to_gpx() zet .fit bestanden om naar .gpx
         (zie FittoGPXwithCheck.py), en filtert daarbij op
         activity_filter.
      3) sync_from_tredict() doet beide stappen achter elkaar en zet
         self.data_source meteen op de resulterende gpx-map.
      4) sync_from_strava() haalt activiteiten rechtstreeks op via de
         Strava API (OAuth) en bouwt daar zelf .gpx bestanden van, via
         de activity-streams endpoint (lat/lon/hoogte/tijd).
      5) sync_from_strava_export() verwerkt een Strava bulk-export
         (activities.csv + originele bestanden).
    """

    # Fiets-gerelateerde sporttypes zoals ze in FIT-bestanden voorkomen
    CYCLING_FIT_SPORTS = {
        "cycling",
        "road_cycling",
        "bike",
        "biking",
        "ride",
    }

    # Hardloop-gerelateerde sporttypes zoals ze in FIT-bestanden voorkomen
    RUNNING_FIT_SPORTS = {
        "running",
        "run",
        "trail_running",
        "track_running",
        "treadmill_running",
        "indoor_running",
    }

    # Fiets-/hardloop-gerelateerde sport_type-waardes zoals de
    # officiële Strava API ze teruggeeft (GET /athlete/activities).
    STRAVA_CYCLING_TYPES = {
        "ride",
        "mountainbikeride",
        "gravelride",
    }
    STRAVA_RUNNING_TYPES = {
        "run",
        "trailrun",
    }

    def __init__(
        self,
        *,
        data_source=None,
        api_key="",
        extent,
        activity_filter="cycling",
        frames_dir="frames",
        fps=25,
        max_duration=30,
        dpi=100,
        figsize=(16.97, 12),
        zoom=12,
        ffmpeg_path=None,
        # ---- Tredict / FIT->GPX instellingen (optioneel) ----
        tredict_token=None,
        tredict_base_url="https://www.tredict.com/api/oauth/v2",
        tredict_page_size=1000,
        tredict_request_delay=0.25,
        tredict_max_retries=4,
        fit_dir="Activities_fit",
        # ---- Strava API instellingen (optioneel) ----
        strava_client_id=None,
        strava_client_secret=None,
        strava_refresh_token=None,
        strava_base_url="https://www.strava.com/api/v3",
        strava_page_size=200,
        strava_request_delay=0.5,
        strava_max_retries=4,
    ):
        """
        data_source: pad naar een map met .gpx bestanden. Optioneel -
        laat leeg (None) om automatisch een map te gebruiken die bij
        activity_filter hoort ("Activities_gpx_cycling" of
        "Activities_gpx_running"). Die map wordt, als hij nog niet
        bestaat, meteen aangemaakt (handig bij een eerste run: dan is
        er nog geen gpx-data en vul je de map daarna met
        sync_from_tredict() / sync_from_strava() /
        sync_from_strava_export()).

        activity_filter: "cycling" (standaard) of "running". Bepaalt
        overal in de klasse welke activiteiten worden geselecteerd:
        Tredict-download, FIT->GPX-conversie, de officiële Strava API,
        de Strava bulk-export-CSV, én (als data_source niet expliciet
        is opgegeven) welke gpx-map wordt gebruikt.

        api_key: Thunderforest API key. Laat deze leeg ("" of None)
        om automatisch terug te vallen op de gratis OpenStreetMap
        (Mapnik) tileserver als achtergrondkaart, zowel bij de video
        (setup_figure) als bij de poster-export (export_a0_map).

        ffmpeg_path is optioneel: build_video() gebruikt standaard de
        ffmpeg-binary die imageio-ffmpeg zelf meelevert (pip install
        imageio-ffmpeg), dus je hoeft normaal geen los ffmpeg-pad meer
        op te geven. Zet ffmpeg_path alleen als je expliciet een
        andere/specifieke ffmpeg-installatie wil forceren.

        tredict_token is optioneel en alleen nodig als je
        download_tredict_activities() / sync_from_tredict()
        gebruikt. Geef hem liever mee via een environment variable
        (bv. os.environ["TREDICT_TOKEN"]) dan hardcoded in je script.

        fit_dir is de map waar gedownloade .fit/.tcx bestanden van
        Tredict worden opgeslagen, voordat ze naar gpx worden omgezet.

        strava_client_id / strava_client_secret / strava_refresh_token
        zijn optioneel en alleen nodig als je
        download_strava_activities() / sync_from_strava()
        gebruikt. Deze horen bij een Strava API-applicatie (aan te
        maken via https://www.strava.com/settings/api); de
        refresh_token haal je één keer op via
        exchange_strava_authorization_code() (zie die docstring).
        Geef ze liever mee via environment variables dan hardcoded in
        je script. Vereist het pakket 'stravalib' (pip install
        stravalib) voor de OAuth-token-uitwisseling.
        """
        if activity_filter not in ("cycling", "running"):
            raise ValueError(
                f"activity_filter moet 'cycling' of 'running' zijn, kreeg: {activity_filter!r}"
            )
        self.activity_filter = activity_filter

        # Geen data_source opgegeven? Gebruik een map die bij de
        # activity_filter hoort, zodat cycling- en running-data nooit
        # per ongeluk door elkaar in dezelfde map belanden.
        if data_source is None:
            data_source = f"Activities_gpx_{activity_filter}"

        # Zorg dat de gpx-map bestaat, ook bij een eerste run waarin
        # er nog geen data is gedownload/geconverteerd.
        Path(data_source).mkdir(parents=True, exist_ok=True)

        self.data_source = data_source
        self.ffmpeg_path = ffmpeg_path
        self.api_key = api_key
        self.xmin, self.ymin, self.xmax, self.ymax = extent
        self.frames_dir = frames_dir
        self.fps = fps
        self.max_duration = max_duration
        self.dpi = dpi
        self.figsize = figsize
        self.zoom = zoom

        # Tredict / FIT->GPX
        self.tredict_token = tredict_token
        self.tredict_base_url = tredict_base_url
        self.tredict_page_size = tredict_page_size
        self.tredict_request_delay = tredict_request_delay
        self.tredict_max_retries = tredict_max_retries
        self.fit_dir = fit_dir
        self._tredict_session = None
        self._fit_converter = None

        # Strava API
        self.strava_client_id = strava_client_id
        self.strava_client_secret = strava_client_secret
        self.strava_refresh_token = strava_refresh_token
        self.strava_base_url = strava_base_url
        self.strava_page_size = strava_page_size
        self.strava_request_delay = strava_request_delay
        self.strava_max_retries = strava_max_retries
        self._strava_session = None
        self._strava_access_token = None
        self._strava_token_expires_at = None

        # wordt later gevuld
        self.gdf = None
        self.lines = []
        self.fig = None
        self.ax = None
        self.line_layers = []
        self.point_layers = []
        self.step = 1
        self.frames = 0
        self._line_coords = []  # precomputed numpy arrays per lijn (snelheid)

    # ==============================================================
    # Tredict: activiteiten downloaden (.fit / .tcx)
    # ==============================================================
    def _get_tredict_session(self):
        if self._tredict_session is None:
            if not self.tredict_token:
                raise RuntimeError(
                    "Geen tredict_token opgegeven. Geef deze mee bij het "
                    "aanmaken van CyclingMovementRenderer(tredict_token=...)."
                )
            session = requests.Session()
            session.headers.update(
                {
                    "Authorization": f"Bearer {self.tredict_token}",
                    "Accept": "application/json",
                }
            )
            self._tredict_session = session
        return self._tredict_session

    def _tredict_get_with_retry(self, url, params=None):
        """
        GET request met retry-logica voor tijdelijke fouten (rate limits,
        server errors, connectie-problemen).
        """
        session = self._get_tredict_session()

        for attempt in range(1, self.tredict_max_retries + 1):
            try:
                response = session.get(url, params=params, timeout=120)

                if response.status_code == 200:
                    return response

                if response.status_code == 429:
                    wait = 2 ** attempt
                    print(f"    Rate limit (429). Wachten {wait}s...")
                    time.sleep(wait)
                    continue

                if response.status_code in {500, 502, 503, 504}:
                    wait = 2 ** attempt
                    print(f"    Server error ({response.status_code}). Wachten {wait}s...")
                    time.sleep(wait)
                    continue

                if response.status_code in {401, 403}:
                    raise RuntimeError(
                        f"Tredict authentication error {response.status_code}: "
                        f"{response.text}"
                    )

                response.raise_for_status()

            except requests.RequestException as e:
                if attempt >= self.tredict_max_retries:
                    raise
                wait = 2 ** attempt
                print(f"    Connection error: {e}")
                print(f"    Retry in {wait}s...")
                time.sleep(wait)

        raise RuntimeError(f"Request failed after {self.tredict_max_retries} attempts.")

    @staticmethod
    def _parse_tredict_date(value):
        """Parse een Tredict UTC ISO timestamp, bv. 2026-08-31T12:34:56.000Z"""
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    @staticmethod
    def _safe_filename(value):
        """Verwijdert tekens die problematisch zijn op Windows."""
        value = str(value)
        value = re.sub(r'[<>:"/\\|?*]', "_", value)
        value = value.rstrip(". ")
        return value.strip()

    def _matches_activity_filter_tredict(self, activity):
        """
        Tredict documenteert sportType-waardes zoals
        cycling/running/swimming/misc - deze komen 1-op-1 overeen met
        self.activity_filter ("cycling"/"running").
        """
        sport_type = (activity.get("sportType") or "").lower()
        return sport_type == self.activity_filter

    def fetch_tredict_activities(self, start_date):
        """
        Haalt activiteiten op van Tredict, van nu terug tot start_date.

        Tredict's activityList is een one-way traversal: nieuw -> oud.
        We gebruiken de 'next' link voor paginering en stoppen zodra we
        een activiteit tegenkomen die ouder is dan start_date.

        start_date: string in formaat "YYYY-MM-DD"
        """
        start_datetime = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        if start_datetime > now:
            raise ValueError("start_date kan niet in de toekomst liggen.")

        activities = []
        url = f"{self.tredict_base_url}/activityList"
        params = {"pageSize": self.tredict_page_size}
        page = 1

        while True:
            print()
            print(f"Fetching activity page {page}...")

            response = self._tredict_get_with_retry(url, params=params)
            data = response.json()
            batch = data.get("_embedded", {}).get("activityList", [])

            if not batch:
                print("No more activities.")
                break

            print(f"  Received {len(batch)} activities.")

            reached_start_date = False
            for activity in batch:
                activity_date = activity.get("date")
                if not activity_date:
                    continue
                try:
                    activity_datetime = self._parse_tredict_date(activity_date)
                except ValueError:
                    print(f"  WARNING: invalid date: {activity_date}")
                    continue

                if activity_datetime < start_datetime:
                    reached_start_date = True
                    break

                activities.append(activity)

            if reached_start_date:
                print()
                print(f"Reached start_date ({start_date}).")
                break

            next_link = data.get("_links", {}).get("next")
            if not next_link:
                print("No next page.")
                break

            next_url = next_link.get("href")
            if not next_url:
                print("No next URL.")
                break

            url = next_url
            params = None
            page += 1
            time.sleep(self.tredict_request_delay)

        return activities

    def _download_tredict_activity(self, activity, fit_dir):
        """
        Downloadt het originele activiteitsbestand (.fit of .tcx) van
        Tredict naar fit_dir. Geeft True terug als er gedownload is,
        False als het bestand al bestond.
        """
        activity_id = activity["id"]
        title = activity.get("title") or activity.get("name") or "cycling"
        date_value = activity.get("date") or "unknown-date"
        timestamp = date_value.replace(":", "-").replace("T", "_").replace("Z", "")
        safe_title = self._safe_filename(title)

        output_file = fit_dir / f"{timestamp}_{safe_title}_{activity_id}.fit"
        if output_file.exists():
            print(f"    Already exists: {output_file.name}")
            return False

        url = f"{self.tredict_base_url}/activity/file/{activity_id}"
        response = self._tredict_get_with_retry(url)

        content_disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)

        extension = ".fit"
        if match:
            original_extension = Path(match.group(1)).suffix.lower()
            if original_extension in {".fit", ".tcx"}:
                extension = original_extension

        output_file = fit_dir / f"{timestamp}_{safe_title}_{activity_id}{extension}"
        if output_file.exists():
            print(f"    Already exists: {output_file.name}")
            return False

        output_file.write_bytes(response.content)
        print(f"    -> {output_file}")
        return True

    def download_tredict_activities(self, start_date, fit_dir=None):
        """
        Haalt activiteiten op van Tredict (vanaf start_date tot nu) die
        overeenkomen met self.activity_filter ("cycling"/"running"), en
        downloadt de originele .fit/.tcx bestanden naar fit_dir
        (standaard self.fit_dir).

        Geeft een dict terug met downloaded/skipped/failed tellers.
        """
        fit_dir = Path(fit_dir or self.fit_dir)
        fit_dir.mkdir(parents=True, exist_ok=True)

        print("=" * 70)
        print(f"TREDICT {self.activity_filter.upper()} ACTIVITY DOWNLOADER")
        print("=" * 70)
        print(f"Start date : {start_date}")
        print(f"Output     : {fit_dir.resolve()}")
        print()

        print("Fetching activities from Tredict...")
        activities = self.fetch_tredict_activities(start_date)

        print()
        print(f"Activities in requested period: {len(activities)}")

        filtered_activities = [a for a in activities if self._matches_activity_filter_tredict(a)]
        print(f"{self.activity_filter.capitalize()} activities: {len(filtered_activities)}")

        result = {"downloaded": 0, "skipped": 0, "failed": 0}

        if not filtered_activities:
            print()
            print(f"No {self.activity_filter} activities found.")
            return result

        print()
        for index, activity in enumerate(filtered_activities, start=1):
            activity_id = activity.get("id", "unknown")
            title = activity.get("title") or activity.get("name") or self.activity_filter
            date_value = activity.get("date", "unknown")

            print(f"[{index}/{len(filtered_activities)}] {date_value} - {title} ({activity_id})")

            try:
                downloaded = self._download_tredict_activity(activity, fit_dir)
                if downloaded:
                    result["downloaded"] += 1
                else:
                    result["skipped"] += 1
            except Exception as e:
                result["failed"] += 1
                print(f"    ERROR: {e}")

            # Tredict documenteert een maximum van 10 downloads per seconde.
            time.sleep(self.tredict_request_delay)

        print()
        print("=" * 70)
        print("DONE")
        print("=" * 70)
        print(f"Activities found : {len(activities)}")
        print(f"{self.activity_filter.capitalize():<16} : {len(filtered_activities)}")
        print(f"Downloaded       : {result['downloaded']}")
        print(f"Skipped          : {result['skipped']}")
        print(f"Failed           : {result['failed']}")
        print(f"Output directory : {fit_dir.resolve()}")

        return result

    # ==============================================================
    # FIT -> GPX conversie
    # ==============================================================
    def _get_fit_converter(self):
        if self._fit_converter is None:
            self._fit_converter = Converter()
        return self._fit_converter

    @staticmethod
    def _get_fit_sport_type(fitfile):
        """Leest het sporttype uit een FIT-file."""
        for record in fitfile.get_messages("sport"):
            values = {field.name: field.value for field in record}
            sport = values.get("sport")
            sub_sport = values.get("sub_sport")
            if sport:
                return str(sport).lower(), str(sub_sport).lower() if sub_sport else None
        return None, None

    def _matches_activity_filter_fit(self, sport, sub_sport):
        """
        Controleert of een FIT-activiteit overeenkomt met
        self.activity_filter ("cycling"/"running").
        """
        if not sport:
            return False

        sport = sport.lower()
        explicit_sports = self.CYCLING_FIT_SPORTS if self.activity_filter == "cycling" else self.RUNNING_FIT_SPORTS
        substring = "cycl" if self.activity_filter == "cycling" else "run"

        if sport in explicit_sports:
            return True
        if substring in sport:
            return True
        if sub_sport and substring in sub_sport:
            return True
        return False

    @staticmethod
    def _fit_has_trackpoints(fitfile):
        """Controleert of een FIT-file GPS-recordpunten bevat."""
        for record in fitfile.get_messages("record"):
            lat = lon = None
            for field in record:
                if field.name == "position_lat":
                    lat = field.value
                elif field.name == "position_long":
                    lon = field.value
            if lat is not None and lon is not None:
                return True
        return False

    def fit_to_gpx(self, fit_path, gpx_path, check_sport=True):
        """
        Zet één .fit bestand om naar .gpx. Gebruikt fit2gpx voor de
        daadwerkelijke conversie. Losse, herbruikbare methode zodat
        andere workflows (Tredict-download, Strava bulk-export-import)
         'm allebei kunnen aanroepen.

        check_sport=True (standaard): sport uit de FIT-file zelf wordt
        gecontroleerd tegen self.activity_filter ("cycling"/"running"),
        en activiteiten van het verkeerde type of zonder trackdata
        worden overgeslagen (return False). Zet dit op False als de
        sport-filtering al elders is gebeurd (bv. op basis van de
        Strava export-CSV) om dubbele/conflicterende filtering te
        voorkomen.
        """
        fitfile = FitFile(str(fit_path))

        if check_sport:
            sport, sub_sport = self._get_fit_sport_type(fitfile)
            if not self._matches_activity_filter_fit(sport, sub_sport):
                print(f"OVERGESLAGEN (geen {self.activity_filter}-activiteit): {fit_path.name}")
                return False

        if not self._fit_has_trackpoints(fitfile):
            print(f"OVERGESLAGEN (geen trackdata): {fit_path.name}")
            return False

        converter = self._get_fit_converter()
        converter.fit_to_gpx(f_in=fit_path, f_out=str(gpx_path))
        print(f"OK: {fit_path.name} -> {gpx_path.name}")
        return True

    def convert_fit_folder_to_gpx(self, fit_dir=None, gpx_dir=None):
        """
        Zet alle .fit bestanden in fit_dir (standaard self.fit_dir) om
        naar .gpx bestanden in gpx_dir (standaard self.data_source, als
        dat een mappad is). Alleen fietsactiviteiten met trackdata
        worden geconverteerd; de rest wordt overgeslagen.

        Geeft een dict terug met converted/skipped/failed tellers.
        """
        fit_dir = Path(fit_dir or self.fit_dir)
        gpx_dir = Path(gpx_dir or self.data_source)
        gpx_dir.mkdir(parents=True, exist_ok=True)

        fit_files = list(fit_dir.glob("*.fit"))
        print(f"Gevonden: {len(fit_files)} FIT-bestanden in {fit_dir}")

        result = {"converted": 0, "skipped": 0, "failed": 0}

        for fit_file in fit_files:
            gpx_file = gpx_dir / f"{fit_file.stem}.gpx"
            try:
                converted = self.fit_to_gpx(fit_file, gpx_file)
                if converted:
                    result["converted"] += 1
                else:
                    result["skipped"] += 1
            except Exception as e:
                result["failed"] += 1
                print(f"FOUT in {fit_file.name}: {e}")

        print(
            f"FIT->GPX klaar: {result['converted']} geconverteerd, "
            f"{result['skipped']} overgeslagen, {result['failed']} mislukt."
        )
        return result

    def sync_from_tredict(self, start_date, fit_dir=None, gpx_dir=None):
        """
        Combineert download_tredict_activities() en
        convert_fit_folder_to_gpx() in één stap: haalt nieuwe
        fietsactiviteiten op van Tredict, zet ze om naar .gpx, en zet
        self.data_source op de resulterende gpx-map zodat run() /
        load_data() deze meteen kan gebruiken.
        """
        fit_dir = Path(fit_dir or self.fit_dir)
        gpx_dir = Path(gpx_dir or self.data_source)

        self.download_tredict_activities(start_date=start_date, fit_dir=fit_dir)
        self.convert_fit_folder_to_gpx(fit_dir=fit_dir, gpx_dir=gpx_dir)

        self.data_source = str(gpx_dir)
        return self

    # ==============================================================
    # Strava bulk export: activities.csv verwerken
    # ==============================================================
    #
    # Werkt op basis van de "bulk export" die je via Strava's
    # accountinstellingen kan aanvragen (niet de live API). Die export
    # bevat een activities.csv met alle activiteiten, plus een map
    # met de originele bestanden per activiteit (.fit/.gpx/.tcx,
    # eventueel .gz-gecomprimeerd).
    #
    # De CSV wordt gelezen met Python's csv-module, die zich houdt aan
    # RFC4180-quoting: een veld dat tussen aanhalingstekens staat mag
    # zelf linefeeds bevatten (bv. de beschrijving van een activiteit)
    # zonder dat dit als een nieuwe rij wordt gezien. Dat lost het
    # "rij begint soms niet met een activity ID"-probleem structureel
    # op, in plaats van dat we zelf regels aan elkaar moeten plakken.

    # Nederlandse en Engelse varianten van de relevante kolomnamen,
    # afhankelijk van de taalinstelling van het Strava-account tijdens
    # de export.
    _STRAVA_CSV_ID_COLUMNS = ("Activity ID", "Activiteits-ID")
    _STRAVA_CSV_TYPE_COLUMNS = ("Activity Type", "Activiteitstype")
    _STRAVA_CSV_FILENAME_COLUMNS = ("Filename", "Bestandsnaam")

    # Trefwoorden (kleine letters) die duiden op een fiets-gerelateerde
    # activiteit, in beide talen. Substring-match, dus dekt ook
    # "Gravelrit"/"Gravel Ride", "Mountainbiken"/"Mountain Bike Ride",
    # "Virtuele fietsrit"/"Virtual Ride", "E-fietsrit"/"E-Bike Ride" etc.
    CYCLING_CSV_KEYWORDS = {
        "ride", "cycling", "bike", "biking",
        "fiets", "wielren", "mountainbik", "mtb",
        "gravel", "ebike", "e-bike", "e-fiets",
        "handcycle", "handbike",
        "velomobile", "velomobiel",
    }

    # Trefwoorden (kleine letters) die duiden op een hardloop-
    # gerelateerde activiteit, in beide talen. Dekt o.a.
    # "Hardlopen"/"Run", "Trailrun"/"Trail hardlopen",
    # "Baanhardlopen"/"Track Run", "Loopband"/"Treadmill Run".
    RUNNING_CSV_KEYWORDS = {
        "run", "running",
        "hardloop", "hardlop", "hardgelopen",
        "trail run", "trailrun", "traillopen", "trailhardlopen",
        "track run", "baanhardlop",
        "treadmill", "loopband",
    }

    @staticmethod
    def _get_csv_field(row, candidates):
        """Geeft de waarde van de eerste aanwezige kolomnaam uit candidates."""
        for name in candidates:
            if name in row and row[name] not in (None, ""):
                return row[name]
        return None

    def _matches_activity_filter_csv_type(self, activity_type):
        """
        Controleert of een activiteitstype uit de export-CSV
        overeenkomt met self.activity_filter ("cycling"/"running").
        """
        if not activity_type:
            return False
        activity_type = activity_type.lower()
        keywords = self.CYCLING_CSV_KEYWORDS if self.activity_filter == "cycling" else self.RUNNING_CSV_KEYWORDS
        return any(keyword in activity_type for keyword in keywords)

    def list_strava_export_activities(self, csv_path):
        """
        Leest een Strava bulk-export activities.csv (Engels of
        Nederlands) en geeft een lijst met dicts terug voor alle
        activiteiten die overeenkomen met self.activity_filter
        ("cycling"/"running"): {"id", "activity_type", "filename"}.

        Gebruikt csv.DictReader (RFC4180-quoting) zodat meerdere
        linefeeds binnen een aangehaald veld (bv. de beschrijving)
        niet per ongeluk als nieuwe rijen worden gelezen.
        """
        csv_path = Path(csv_path)

        # newline="" is vereist door de csv-module voor correcte
        # afhandeling van aangehaalde linefeeds/CRLF's.
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)

            if reader.fieldnames is None:
                raise ValueError(f"Kon geen header vinden in {csv_path}.")

            id_column = next((c for c in self._STRAVA_CSV_ID_COLUMNS if c in reader.fieldnames), None)
            type_column = next((c for c in self._STRAVA_CSV_TYPE_COLUMNS if c in reader.fieldnames), None)
            filename_column = next((c for c in self._STRAVA_CSV_FILENAME_COLUMNS if c in reader.fieldnames), None)

            if not id_column or not type_column:
                raise ValueError(
                    f"Verwachte kolommen niet gevonden in {csv_path}. "
                    f"Gevonden kolommen: {reader.fieldnames}"
                )

            activities = []
            skipped_no_id = 0

            for row in reader:
                activity_id = self._get_csv_field(row, (id_column,))
                if not activity_id:
                    # Zou met een correcte csv-reader niet meer moeten
                    # voorkomen (linefeeds binnen quotes worden al
                    # correct afgehandeld), maar als vangnet: rijen
                    # zonder ID overslaan i.p.v. laten crashen.
                    skipped_no_id += 1
                    continue

                activity_type = self._get_csv_field(row, (type_column,))
                if not self._matches_activity_filter_csv_type(activity_type):
                    continue

                filename = self._get_csv_field(row, (filename_column,)) if filename_column else None

                activities.append(
                    {
                        "id": str(activity_id).strip(),
                        "activity_type": activity_type,
                        "filename": filename.strip() if filename else None,
                    }
                )

        if skipped_no_id:
            print(f"Waarschuwing: {skipped_no_id} rij(en) zonder activity ID overgeslagen.")

        print(f"Gevonden in {csv_path.name}: {len(activities)} {self.activity_filter}-activiteiten.")
        return activities

    @staticmethod
    def _maybe_decompress(src_path, work_dir):
        """
        Als src_path een .gz bestand is, pakt het uit naar work_dir
        (dezelfde bestandsnaam zonder .gz) en geeft dat pad terug.
        Anders wordt src_path ongewijzigd teruggegeven.
        """
        src_path = Path(src_path)
        if src_path.suffix.lower() != ".gz":
            return src_path

        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        dest_path = work_dir / src_path.stem  # strip alleen de .gz

        with gzip.open(src_path, "rb") as f_in, open(dest_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        return dest_path

    @staticmethod
    def _strip_xml_ns(tag):
        """Haalt een eventueel XML-namespace-voorvoegsel ({...}) van een tag af."""
        return tag.split("}", 1)[-1] if "}" in tag else tag

    def tcx_to_gpx(self, tcx_path, gpx_path):
        """
        Zet één .tcx bestand (Garmin Training Center XML) om naar
        .gpx. Leest Trackpoints (tijd, lat/lon, hoogte) uit alle
        Laps/Tracks in het bestand en zet ze om in een GPX-track met
        één segment per Lap. Namespace-agnostisch (verschillende TCX-
        exports gebruiken soms een andere xmlns), dus we matchen op
        de lokale tag-naam i.p.v. de volledige namespace.
        """
        tcx_path = Path(tcx_path)
        gpx_path = Path(gpx_path)

        tree = ET.parse(tcx_path)
        root = tree.getroot()

        gpx = gpxpy.gpx.GPX()
        track = gpxpy.gpx.GPXTrack(name=tcx_path.stem)
        gpx.tracks.append(track)

        found_any_point = False

        for activity_el in root.iter():
            if self._strip_xml_ns(activity_el.tag) != "Activity":
                continue

            for lap_el in activity_el:
                if self._strip_xml_ns(lap_el.tag) != "Lap":
                    continue

                for track_el in lap_el:
                    if self._strip_xml_ns(track_el.tag) != "Track":
                        continue

                    segment = gpxpy.gpx.GPXTrackSegment()

                    for trackpoint_el in track_el:
                        if self._strip_xml_ns(trackpoint_el.tag) != "Trackpoint":
                            continue

                        lat = lon = ele = None
                        point_time = None

                        for field_el in trackpoint_el:
                            tag = self._strip_xml_ns(field_el.tag)

                            if tag == "Time" and field_el.text:
                                try:
                                    point_time = datetime.fromisoformat(
                                        field_el.text.replace("Z", "+00:00")
                                    )
                                except ValueError:
                                    point_time = None

                            elif tag == "Position":
                                for pos_el in field_el:
                                    pos_tag = self._strip_xml_ns(pos_el.tag)
                                    if pos_tag == "LatitudeDegrees" and pos_el.text:
                                        lat = float(pos_el.text)
                                    elif pos_tag == "LongitudeDegrees" and pos_el.text:
                                        lon = float(pos_el.text)

                            elif tag == "AltitudeMeters" and field_el.text:
                                try:
                                    ele = float(field_el.text)
                                except ValueError:
                                    ele = None

                        if lat is not None and lon is not None:
                            segment.points.append(
                                gpxpy.gpx.GPXTrackPoint(lat, lon, elevation=ele, time=point_time)
                            )
                            found_any_point = True

                    if segment.points:
                        track.segments.append(segment)

        if not found_any_point:
            raise ValueError(f"Geen GPS-trackpoints gevonden in {tcx_path.name}.")

        gpx_path.write_text(gpx.to_xml(), encoding="utf-8")
        return True

    def import_strava_bulk_export(
        self,
        csv_path,
        activities_dir="StravaExport/activities",
        gpx_dir=None,
        work_dir=None,
    ):
        """
        Verwerkt een volledige Strava bulk-export:
          1. Leest csv_path (activities.csv, Engels of Nederlands) en
             filtert de activiteiten volgens self.activity_filter
             ("cycling"/"running").
          2. Zoekt per activiteit het bijhorende bronbestand
             (.fit/.gpx/.tcx, eventueel .gz-gecomprimeerd) in
             activities_dir.
          3. Pakt het zo nodig uit en converteert het naar .gpx in
             gpx_dir (standaard self.data_source):
               - .fit  -> fit_to_gpx() (check_sport=False, want de
                          sport is al gefilterd via de CSV)
               - .gpx  -> alleen uitpakken/kopiëren
               - .tcx  -> tcx_to_gpx()

        work_dir is een tijdelijke map voor uitgepakte .gz bestanden
        (standaard een auto-opgeruimde temp-map).

        Geeft een dict terug met converted/skipped/failed tellers.
        """
        csv_path = Path(csv_path)
        activities_dir = Path(activities_dir)
        gpx_dir = Path(gpx_dir or self.data_source)
        gpx_dir.mkdir(parents=True, exist_ok=True)

        matched_activities = self.list_strava_export_activities(csv_path)

        result = {"converted": 0, "skipped": 0, "failed": 0}

        use_temp_dir = work_dir is None
        work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="strava_export_"))
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            for index, activity in enumerate(matched_activities, start=1):
                activity_id = activity["id"]
                filename = activity["filename"]

                print(
                    f"[{index}/{len(matched_activities)}] {activity_id} - "
                    f"{activity['activity_type']} ({filename})"
                )

                if not filename:
                    print("    OVERGESLAGEN: geen bestandsnaam in de export.")
                    result["skipped"] += 1
                    continue

                # De CSV verwijst meestal naar "activities/<bestand>";
                # we pakken alleen de bestandsnaam en zoeken die in
                # activities_dir, met een fallback op het volledige
                # (relatieve) pad zoals in de CSV staat.
                src_path = activities_dir / Path(filename).name
                if not src_path.exists():
                    fallback_path = Path(filename)
                    if not fallback_path.is_absolute():
                        fallback_path = activities_dir.parent / filename
                    if fallback_path.exists():
                        src_path = fallback_path

                if not src_path.exists():
                    print(f"    FOUT: bronbestand niet gevonden ({filename}).")
                    result["failed"] += 1
                    continue

                gpx_output = gpx_dir / f"{activity_id}.gpx"
                if gpx_output.exists():
                    print(f"    Already exists: {gpx_output.name}")
                    result["skipped"] += 1
                    continue

                try:
                    extracted_path = self._maybe_decompress(src_path, work_dir)
                    suffix = extracted_path.suffix.lower()

                    if suffix == ".fit":
                        converted = self.fit_to_gpx(extracted_path, gpx_output, check_sport=False)
                        if not converted:
                            result["skipped"] += 1
                            continue
                    elif suffix == ".gpx":
                        shutil.copy2(extracted_path, gpx_output)
                        print(f"    OK: {src_path.name} -> {gpx_output.name}")
                    elif suffix == ".tcx":
                        self.tcx_to_gpx(extracted_path, gpx_output)
                        print(f"    OK: {src_path.name} -> {gpx_output.name}")
                    else:
                        print(f"    OVERGESLAGEN: onbekende extensie '{suffix}'.")
                        result["skipped"] += 1
                        continue

                    result["converted"] += 1

                except Exception as e:
                    result["failed"] += 1
                    print(f"    ERROR: {e}")
        finally:
            if use_temp_dir:
                shutil.rmtree(work_dir, ignore_errors=True)

        print(
            f"Strava bulk-export import klaar: {result['converted']} geconverteerd, "
            f"{result['skipped']} overgeslagen, {result['failed']} mislukt."
        )
        return result

    def sync_from_strava_export(self, csv_path, activities_dir="StravaExport/activities", gpx_dir=None):
        """
        Voert import_strava_bulk_export() uit en zet self.data_source
        op de resulterende gpx-map, zodat run() / load_data() deze
        meteen kan gebruiken.
        """
        gpx_dir = Path(gpx_dir or self.data_source)
        self.import_strava_bulk_export(csv_path=csv_path, activities_dir=activities_dir, gpx_dir=gpx_dir)
        self.data_source = str(gpx_dir)
        return self

    # ==============================================================
    # Strava: activiteiten lijsten en downloaden (volledig via de
    # officiële API)
    # ==============================================================
    #
    # Zowel het lijsten van activiteiten als het ophalen van de
    # GPS-data gebeurt via de officiële, gedocumenteerde Strava API
    # met OAuth (client_id/client_secret/refresh_token):
    #   - GET /athlete/activities voor de lijst met activiteiten;
    #   - GET /activities/{id}/streams voor de lat/lng/hoogte/tijd-
    #     data van één activiteit, waar we zelf een .gpx bestand van
    #     opbouwen (Strava biedt geen directe .gpx-download-endpoint
    #     aan in de officiële API).
    #
    # Let op:
    #   - Voor eigen private activiteiten heeft je Strava API-app de
    #     OAuth-scope "activity:read_all" nodig (i.p.v. alleen
    #     "activity:read"), anders krijg je alleen streams van
    #     publieke activiteiten terug.
    #   - Strava's officiële API is aan rate limits gebonden
    #     (standaard 100 requests/15 min en 1000/dag per app). Bij
    #     veel activiteiten kan het ophalen van streams (1 request
    #     per activiteit) dus wat tijd kosten; de retry-logica hieronder
    #     wacht automatisch bij een 429.
    def _strava_get_with_retry(self, url, params=None):
        """
        GET request naar de Strava API met retry-logica voor rate
        limits (429), tijdelijke server-fouten (5xx), verbindings-
        problemen, en een eenmalige token-refresh bij een verlopen
        access_token (401). Andere clientfouten (bv. 403 door een
        ontbrekende OAuth-scope) worden niet blijvend geretried, en
        de laatste response wordt in de foutmelding meegegeven zodat
        de echte oorzaak zichtbaar is i.p.v. een generieke melding.
        """
        access_token = self._get_strava_access_token()
        headers = {"Authorization": f"Bearer {access_token}"}

        last_response = None
        token_refreshed = False

        for attempt in range(1, self.strava_max_retries + 1):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=60)
            except requests.RequestException as e:
                if attempt >= self.strava_max_retries:
                    raise
                wait = 2 ** attempt
                print(f"    Connection error: {e}")
                print(f"    Retry in {wait}s...")
                time.sleep(wait)
                continue

            last_response = response

            if response.status_code == 401 and not token_refreshed:
                # Access_token blijkbaar verlopen/ongeldig: forceer één
                # keer een nieuwe refresh en probeer opnieuw. Blijft
                # het daarna nog 401, dan is het geen verlopen token
                # maar iets structureels (bv. verkeerde client_id/
                # client_secret/refresh_token) en stoppen we ermee.
                print("    Access token verlopen/ongeldig (401). Token verversen...")
                self._strava_access_token = None
                self._strava_token_expires_at = None
                access_token = self._get_strava_access_token()
                headers = {"Authorization": f"Bearer {access_token}"}
                token_refreshed = True
                continue

            if response.status_code == 429:
                wait = 15 * attempt
                print(f"    Rate limit (429). Wachten {wait}s...")
                time.sleep(wait)
                continue

            if response.status_code in {500, 502, 503, 504}:
                wait = 2 ** attempt
                print(f"    Server error ({response.status_code}). Wachten {wait}s...")
                time.sleep(wait)
                continue

            if response.status_code >= 400:
                # Andere clientfout (401 na refresh, 403 door
                # ontbrekende scope, 404, etc): retryen heeft geen zin,
                # direct stoppen zodat de echte fout zichtbaar wordt.
                break

            return response

        detail = ""
        if last_response is not None:
            detail = f" (laatste status: {last_response.status_code}, body: {last_response.text[:300]})"
        raise RuntimeError(f"Strava request mislukt: {url}{detail}")

    @staticmethod
    def exchange_strava_authorization_code(client_id, client_secret, code):
        """
        Eenmalige helper om een OAuth authorization 'code' (verkregen
        via de browser-autorisatiestap) om te wisselen voor een
        refresh_token. Nodig als je een RuntimeError krijgt met
        "activity:read_permission missing" - dat betekent dat je
        huidige refresh_token zonder de juiste scope is aangemaakt.

        Gebruikt stravalib (pip install stravalib) voor de daadwerkelijke
        uitwisseling.

        Stappen:
          1. Bezoek in de browser (met je eigen client_id, en
             scope=activity:read_all voor ook private activiteiten,
             of activity:read voor alleen publieke):
             https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost&response_type=code&approval_prompt=force&scope=activity:read_all
          2. Log in / autoriseer de app. Je wordt doorgestuurd naar
             een niet-bestaande localhost-pagina; kopieer de
             "code=..." waarde uit de adresbalk (tot aan de
             eerstvolgende "&", laat "&scope=..." erna weg).
          3. Roep deze methode meteen aan met die verse code (codes
             zijn eenmalig en maar enkele minuten geldig):
             CyclingMovementRenderer.exchange_strava_authorization_code(
                 client_id="...", client_secret="...", code="...",
             )
          4. Bewaar de geprinte refresh_token (bv. als
             STRAVA_REFRESH_TOKEN environment variable) en gebruik
             die voortaan.
        """
        try:
            from stravalib import Client
        except ImportError as e:
            raise ImportError(
                "stravalib is niet geïnstalleerd. Installeer het met "
                "'pip install stravalib' om deze helper te gebruiken."
            ) from e

        client = Client()
        try:
            token_response = client.exchange_code_for_token(
                client_id=client_id, client_secret=client_secret, code=code
            )
        except Exception as e:
            raise RuntimeError(
                f"Code-uitwisseling mislukt: {e}\n"
                "Meest voorkomende oorzaken: de code is al één keer "
                "gebruikt, verlopen (codes zijn maar enkele minuten "
                "geldig), fout gekopieerd (bv. mét een '&scope=...' "
                "staartje erachter), of client_id/client_secret horen "
                "niet bij dezelfde Strava API-app. Doorloop stap 1-2 "
                "opnieuw voor een verse code."
            ) from e

        print("Nieuwe refresh_token:", token_response["refresh_token"])
        print("Toegekende scopes staan in de 'scope' query-parameter van de redirect-URL uit stap 2.")
        return token_response

    def _get_strava_access_token(self):
        """
        Wisselt de refresh_token om voor een tijdelijk access_token via
        stravalib (pip install stravalib). Het token wordt gecached tot
        vlak voor het verloopt, zodat niet bij elke aanroep een nieuw
        token wordt opgehaald.
        """
        if not (self.strava_client_id and self.strava_client_secret and self.strava_refresh_token):
            raise RuntimeError(
                "Geef strava_client_id, strava_client_secret en "
                "strava_refresh_token mee bij het aanmaken van "
                "CyclingMovementRenderer(...) om de Strava API te gebruiken."
            )

        now = time.time()
        if self._strava_access_token and self._strava_token_expires_at and now < self._strava_token_expires_at - 60:
            return self._strava_access_token

        try:
            from stravalib import Client
        except ImportError as e:
            raise ImportError(
                "stravalib is niet geïnstalleerd. Installeer het met "
                "'pip install stravalib' om de Strava-integratie te gebruiken."
            ) from e

        client = Client()
        try:
            token_response = client.refresh_access_token(
                client_id=self.strava_client_id,
                client_secret=self.strava_client_secret,
                refresh_token=self.strava_refresh_token,
            )
        except Exception as e:
            raise RuntimeError(
                f"Ophalen van Strava access_token mislukt: {e}\n"
                "Meest voorkomende oorzaken: strava_refresh_token is "
                "onjuist/verouderd (bv. per ongeluk het token van "
                "strava.com/settings/api, of een niet meer geldige "
                "refresh_token - refresh_tokens kunnen wijzigen bij "
                "gebruik), of strava_client_id/strava_client_secret "
                "horen niet bij dezelfde Strava API-app."
            ) from e

        self._strava_access_token = token_response["access_token"]
        self._strava_token_expires_at = token_response.get("expires_at", now + 3600)

        # Strava kan bij het verversen soms een nieuwe refresh_token
        # teruggeven die de oude vervangt; die pakken we dan meteen
        # over zodat een volgende refresh niet op een verouderde
        # (ongeldige) token stukloopt.
        new_refresh_token = token_response.get("refresh_token")
        if new_refresh_token and new_refresh_token != self.strava_refresh_token:
            print(
                "Let op: Strava gaf een nieuwe refresh_token terug. "
                f"Update strava_refresh_token naar: {new_refresh_token}"
            )
            self.strava_refresh_token = new_refresh_token

        return self._strava_access_token

    def list_strava_activities(self, start_date, filter_by_mode=True):
        """
        Haalt via de officiële Strava API alle activiteiten op vanaf
        start_date ("YYYY-MM-DD") tot nu, met paginering.

        filter_by_mode=True (standaard) filtert op sport_type-waardes
        die overeenkomen met self.activity_filter ("cycling": Ride,
        GravelRide, MountainBikeRide, VirtualRide, etc; "running":
        Run, TrailRun, VirtualRun). Zet op False om alle activiteiten
        terug te krijgen, ongeacht sporttype.

        Geeft een lijst met dicts terug: {"id", "name", "start_date",
        "sport_type"}.
        """
        start_datetime = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        after_epoch = int(start_datetime.timestamp())

        mode_types = self.STRAVA_CYCLING_TYPES if self.activity_filter == "cycling" else self.STRAVA_RUNNING_TYPES

        activities = []
        page = 1

        while True:
            print(f"Fetching Strava activity page {page}...")

            response = self._strava_get_with_retry(
                f"{self.strava_base_url}/athlete/activities",
                params={"after": after_epoch, "per_page": self.strava_page_size, "page": page},
            )
            batch = response.json()

            if not batch:
                print("No more activities.")
                break

            print(f"  Received {len(batch)} activities.")

            for activity in batch:
                sport_type = (activity.get("sport_type") or activity.get("type") or "").lower()

                if filter_by_mode and sport_type not in mode_types:
                    continue

                activities.append(
                    {
                        "id": activity["id"],
                        "name": activity.get("name", "activity"),
                        "start_date": activity.get("start_date"),
                        "sport_type": activity.get("sport_type") or activity.get("type"),
                    }
                )

            if len(batch) < self.strava_page_size:
                break

            page += 1
            time.sleep(self.strava_request_delay)

        mode_label = f"{self.activity_filter}-" if filter_by_mode else ""
        print(f"Totaal gevonden: {len(activities)} {mode_label}activiteiten sinds {start_date}.")
        return activities

    def _get_strava_activity_streams(self, activity_id):
        """
        Haalt de lat/lng-, hoogte- en tijd-streams van één activiteit
        op via de officiële Strava API.
        """
        response = self._strava_get_with_retry(
            f"{self.strava_base_url}/activities/{activity_id}/streams",
            params={"keys": "latlng,altitude,time", "key_by_type": "true"},
        )
        return response.json()

    @staticmethod
    def _strava_streams_to_gpx(activity, streams, output_path):
        """
        Bouwt een .gpx bestand op basis van de lat/lng/altitude/time
        streams van een Strava-activiteit (officiële API). Strava
        biedt zelf geen directe .gpx-download aan in de API, dus we
        zetten de streams hier zelf om met gpxpy.
        """
        latlng_stream = streams.get("latlng", {})
        latlng = latlng_stream.get("data") if isinstance(latlng_stream, dict) else None

        if not latlng:
            raise ValueError(
                "Geen GPS-data (latlng-stream) beschikbaar voor deze activiteit "
                "(bv. een indoor/virtuele rit zonder GPS)."
            )

        altitude_stream = streams.get("altitude", {})
        altitude = altitude_stream.get("data") if isinstance(altitude_stream, dict) else None

        time_stream = streams.get("time", {})
        time_offsets = time_stream.get("data") if isinstance(time_stream, dict) else None

        start_date = activity.get("start_date")  # ISO8601 UTC, bv 2026-08-31T08:32:11Z
        start_dt = None
        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))

        gpx = gpxpy.gpx.GPX()
        track = gpxpy.gpx.GPXTrack(name=activity.get("name", "Strava activity"))
        gpx.tracks.append(track)
        segment = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(segment)

        for i, point in enumerate(latlng):
            lat, lon = point
            ele = altitude[i] if altitude and i < len(altitude) else None

            point_time = None
            if start_dt is not None and time_offsets and i < len(time_offsets):
                point_time = start_dt + timedelta(seconds=time_offsets[i])

            segment.points.append(
                gpxpy.gpx.GPXTrackPoint(lat, lon, elevation=ele, time=point_time)
            )

        output_path.write_text(gpx.to_xml(), encoding="utf-8")
        return True

    def download_strava_activities(self, start_date, gpx_dir=None):
        """
        Haalt fietsactiviteiten op van Strava (volledig via de
        officiële API: activiteitenlijst + streams) en schrijft ze
        weg als .gpx bestanden in gpx_dir (standaard self.data_source).

        Geeft een dict terug met downloaded/skipped/failed tellers.
        """
        gpx_dir = Path(gpx_dir or self.data_source)
        gpx_dir.mkdir(parents=True, exist_ok=True)

        activities = self.list_strava_activities(start_date, filter_by_mode=True)

        result = {"downloaded": 0, "skipped": 0, "failed": 0}

        for index, activity in enumerate(activities, start=1):
            activity_id = activity["id"]
            output_file = gpx_dir / f"{activity_id}.gpx"

            print(f"[{index}/{len(activities)}] {activity['start_date']} - {activity['name']} ({activity_id})")

            if output_file.exists():
                print(f"    Already exists: {output_file.name}")
                result["skipped"] += 1
                continue

            try:
                streams = self._get_strava_activity_streams(activity_id)
                self._strava_streams_to_gpx(activity, streams, output_file)
                print(f"    -> {output_file}")
                result["downloaded"] += 1
            except Exception as e:
                result["failed"] += 1
                print(f"    ERROR: {e}")

            time.sleep(self.strava_request_delay)

        print(
            f"Strava download klaar: {result['downloaded']} gedownload, "
            f"{result['skipped']} overgeslagen, {result['failed']} mislukt."
        )
        return result

    def sync_from_strava(self, start_date, gpx_dir=None):
        """
        Haalt nieuwe fietsactiviteiten op van Strava (via de officiële
        API) en zet self.data_source op de resulterende gpx-map,
        zodat run() / load_data() deze meteen kan gebruiken.
        """
        gpx_dir = Path(gpx_dir or self.data_source)
        self.download_strava_activities(start_date=start_date, gpx_dir=gpx_dir)
        self.data_source = str(gpx_dir)
        return self

    # ------------------------------------------------------------
    # Data
    # ------------------------------------------------------------
    def load_data(self):
        """
        Leest alle .gpx bestanden in self.data_source in. Vult
        self.lines met LineStrings in EPSG:3857.
        """
        gdf = self._read_gpx_folder(self.data_source)

        gdf = gdf.to_crs(3857)
        self.gdf = gdf

        lines = []
        for geom in gdf.geometry:
            if geom is None:
                continue
            if geom.geom_type == "LineString":
                lines.append(geom)
            elif geom.geom_type == "MultiLineString":
                lines.extend(list(geom.geoms))

        self.lines = lines
        # Coords één keer voorberekenen als numpy arrays i.p.v. elke
        # frame opnieuw list(line.coords) op te bouwen (grote speedup
        # bij lange tracks).
        self._line_coords = [np.array(line.coords) for line in lines]
        return self

    @staticmethod
    def _read_gpx_folder(folder_path, layer="tracks"):
        """
        Leest alle .gpx bestanden in een map in en combineert ze tot
        één GeoDataFrame. Standaard wordt de 'tracks' layer gebruikt
        (LineString/MultiLineString per track). Bestanden zonder
        tracks worden overgeslagen.
        """
        gpx_files = sorted(glob.glob(os.path.join(folder_path, "*.gpx")))
        if not gpx_files:
            raise FileNotFoundError(
                f"Geen .gpx bestanden gevonden in {folder_path}. "
                "Eerste keer dat je dit draait? Vul deze map eerst met "
                "sync_from_tredict(), sync_from_strava() of "
                "sync_from_strava_export() voordat je load_data()/run() "
                "aanroept."
            )

        gdfs = []
        for gpx_file in gpx_files:
            try:
                gdf = gpd.read_file(gpx_file, layer=layer)
            except Exception as e:
                print(f"Waarschuwing: kon {gpx_file} niet lezen ({e}), overgeslagen.")
                continue
            if not gdf.empty:
                gdf["source_file"] = os.path.basename(gpx_file)
                gdfs.append(gdf)

        if not gdfs:
            raise ValueError(f"Geen bruikbare tracks gevonden in {folder_path}")

        combined = pd.concat(gdfs, ignore_index=True)
        return gpd.GeoDataFrame(combined, geometry="geometry", crs=gdfs[0].crs)

    # ------------------------------------------------------------
    # Figure + basemap
    # ------------------------------------------------------------
    def _get_tile_source(self):
        """
        Geeft de basemap-bron terug voor contextily. Als er geen
        api_key is opgegeven (leeg of None), wordt teruggevallen op de
        gratis OsmAnd HD-tileserver. Anders wordt de Thunderforest
        Atlas-stijl gebruikt met de opgegeven api_key.
        """
        if not self.api_key:
            return "https://tile.osmand.net/hd/{z}/{x}/{y}.png"

        return (
            "https://api.thunderforest.com/"
            "atlas/{z}/{x}/{y}@2x.png"
            f"?apikey={self.api_key}"
        )

    def setup_figure(self):
        self.fig = plt.figure(figsize=self.figsize, dpi=self.dpi)
        # Volledig full-page axes, net als bij export_a0_map(). Dit
        # voorkomt dat de as-box afhankelijk van de dpi anders wordt
        # opgesteld (zoals fig.tight_layout() deed), wat bij hogere
        # dpi de basemap kon uitrekken en tekst relatief te groot
        # liet lijken.
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(self.xmin, self.xmax)
        self.ax.set_ylim(self.ymin, self.ymax)

        ctx.add_basemap(
            self.ax,
            source=self._get_tile_source(),
            crs="EPSG:3857",
            zoom=self.zoom,
            interpolation="bilinear",
            reset_extent=False,
            attribution=False,
        )
        self.ax.set_axis_off()
        return self

    # ------------------------------------------------------------
    # Track- en punt-layers
    # ------------------------------------------------------------
    def create_layers(self):
        self.line_layers = []
        self.point_layers = []
        for _ in self.lines:
            track_layer, = self.ax.plot(
                [], [], color="red", linewidth=1, alpha=0.9
            )
            point_layer = self.ax.scatter(
                [], [], color="blue", s=20, zorder=10
            )
            self.line_layers.append(track_layer)
            self.point_layers.append(point_layer)
        return self

    # ------------------------------------------------------------
    # "Last Updated" label
    # ------------------------------------------------------------
    def add_last_updated_label(self, today=None):
        today = today or date.today().strftime("%Y-%m-%d")
        self.ax.text(
            0.02, 0.98,
            f"Last Updated: {today}",
            transform=self.ax.transAxes,
            ha="left",
            va="top",
            fontsize=14,
            zorder=20,
            bbox=dict(
                boxstyle="round,pad=0.4",
                facecolor="lightgrey",
                edgecolor="none",
                alpha=0.6,
            ),
        )
        return self

    # ------------------------------------------------------------
    # Aantal frames bepalen
    # ------------------------------------------------------------
    def compute_frame_count(self):
        max_frames = self.fps * self.max_duration
        max_vertices = max(len(line.coords) for line in self.lines)

        self.step = max(1, int(max_vertices / max_frames))
        self.frames = int(max_vertices / self.step)
        return self

    # ------------------------------------------------------------
    # Eén frame tekenen
    # ------------------------------------------------------------
    def draw_frame(self, frame):
        current_index = frame * self.step
        for j, coords in enumerate(self._line_coords):
            n = min(current_index, len(coords))
            if n > 0:
                xy = coords[:n]
                self.line_layers[j].set_data(xy[:, 0], xy[:, 1])

                if current_index < len(coords):
                    # Route is nog bezig: stip op het huidige punt tonen.
                    self.point_layers[j].set_offsets(xy[-1:])
                else:
                    # Route is klaar getekend: stip laten verdwijnen,
                    # lijn blijft wel volledig zichtbaar staan.
                    self.point_layers[j].set_offsets(np.empty((0, 2)))

    # ------------------------------------------------------------
    # Alle frames wegschrijven als JPG
    # ------------------------------------------------------------
    def render_frames(self):
        """
        Rendert alle frames en schrijft ze as JPG weg. Gebruikt
        matplotlib-blitting: de statische achtergrond (basemap + label)
        wordt één keer gerenderd en gecached; per frame wordt alleen
        de gewijzigde lijnen/punten opnieuw getekend en in die
        gecachte achtergrond geplakt. Dat is veel sneller dan elke
        frame de volledige figuur (incl. basemap-tiles) opnieuw op te
        bouwen via fig.savefig().
        """
        os.makedirs(self.frames_dir, exist_ok=True)

        canvas = self.fig.canvas
        canvas.draw()
        # Achtergrond (basemap, "Last Updated"-label, etc.) cachen
        background = canvas.copy_from_bbox(self.fig.bbox)

        for frame in range(self.frames):
            if frame % 25 == 0 or frame == self.frames - 1:
                print(f"Rendering frame {frame + 1}/{self.frames}")

            self.draw_frame(frame)

            canvas.restore_region(background)
            for artist in (*self.line_layers, *self.point_layers):
                self.ax.draw_artist(artist)
            canvas.blit(self.fig.bbox)

            # Framebuffer direct als array pakken en met PIL wegschrijven
            # (sneller dan fig.savefig, dat opnieuw door de hele
            # matplotlib-backend gaat).
            buf = np.asarray(canvas.buffer_rgba())
            img = Image.fromarray(buf[:, :, :3])  # alpha kanaal droppen voor JPG
            img.save(
                os.path.join(self.frames_dir, f"frame_{frame:05d}.jpg"),
                quality=90,
            )

        plt.close(self.fig)
        return self

    # ------------------------------------------------------------
    # Video bouwen met ffmpeg
    # ------------------------------------------------------------
    def build_video(self, output_video):
        """
        Bouwt de video van de gerenderde JPG-frames met imageio +
        imageio-ffmpeg (pip install imageio imageio-ffmpeg). Dit
        gebruikt een door pip meegeleverde ffmpeg-binary, dus geen
        handmatig ffmpeg-pad meer nodig.

        output_video: bestandsnaam/pad van de te schrijven video
        (bv. "cycling_movement2.mp4"). Verplicht - net als output_pdf
        bij export_a0_map() en output_html bij export_folium_map(),
        heeft dit geen default.
        """
        frame_files = sorted(glob.glob(os.path.join(self.frames_dir, "frame_*.jpg")))
        if not frame_files:
            raise FileNotFoundError(
                f"Geen frames gevonden in {self.frames_dir}. "
                "Roep eerst render_frames() aan."
            )

        print(f"Video opbouwen uit {len(frame_files)} frames...")
        writer_kwargs = dict(
            fps=self.fps,
            codec="libx264",
            pixelformat="yuv420p",
            output_params=["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"],
            # We forceren zelf al even breedte/hoogte via bovenstaande
            # -vf filter (voldoende voor yuv420p/h264). Zonder dit zou
            # imageio-ffmpeg óók automatisch proberen te resizen naar
            # een veelvoud van 16, wat botst met onze eigen -vf optie
            # ("Multiple -filter" warning) en tot verwarrende/dubbele
            # scaling kan leiden.
            macro_block_size=1,
        )
        if self.ffmpeg_path:
            writer_kwargs["ffmpeg_params"] = []  # placeholder, zie opmerking hieronder
            os.environ["IMAGEIO_FFMPEG_EXE"] = self.ffmpeg_path

        with imageio.get_writer(output_video, **writer_kwargs) as writer:
            for frame_file in frame_files:
                writer.append_data(imageio.imread(frame_file))

        print(f"Done: {output_video}")
        return self

    # ------------------------------------------------------------
    # A0 poster export (los van de video-render)
    # ------------------------------------------------------------
    def export_a0_map(
        self,
        output_pdf="a0_map.pdf",
        zoom=14,
        dpi=300,
        line_color="red",
        line_width=1.5,
        margin=0.05,
    ):
        """
        Exporteert self.gdf (alle lijnen, volledig getekend) als een
        A0-poster PDF over de basemap. Vereist dat load_data() al is
        aangeroepen.
        """
        if self.gdf is None:
            raise RuntimeError("Roep eerst load_data() aan voordat je export_a0_map() gebruikt.")

        if self.gdf.empty:
            raise ValueError("De GeoDataFrame is leeg.")

        lines = self.gdf[self.gdf.geometry.notna()].copy()
        lines = lines.to_crs(epsg=3857)

        # ------------------------------------------------------------
        # A0 afmetingen in inches (841 x 1189 mm), liggend
        # ------------------------------------------------------------
        a0_width = 1189 / 25.4
        a0_height = 841 / 25.4
        a0_aspect_ratio = a0_width / a0_height

        # Extent met marge rondom de opgegeven bounding box
        width = self.xmax - self.xmin
        height = self.ymax - self.ymin

        xmin = self.xmin - width * margin
        xmax = self.xmax + width * margin
        ymin = self.ymin - height * margin
        ymax = self.ymax + height * margin

        # Vul de kortste dimensie aan zodat de kaart niet vervormt op
        # het A0-landscape papier. De extent blijft gecentreerd.
        width = xmax - xmin
        height = ymax - ymin
        extent_aspect_ratio = width / height
        center_x = (xmin + xmax) / 2
        center_y = (ymin + ymax) / 2
        

        if extent_aspect_ratio > a0_aspect_ratio:
            height = width / a0_aspect_ratio
        else:
            width = height * a0_aspect_ratio

        xmin = center_x - width / 2
        xmax = center_x + width / 2
        ymin = center_y - height / 2
        ymax = center_y + height / 2

        # ------------------------------------------------------------
        # Figuur op A0-formaat
        # ------------------------------------------------------------
        fig = plt.figure(figsize=(a0_width, a0_height), dpi=dpi)

        # Volledig full-page axes
        ax = fig.add_axes([0, 0, 1, 1])

        # Extent instellen VOORDAT de basemap wordt toegevoegd
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)

        ctx.add_basemap(
            ax,
            source=self._get_tile_source(),
            crs="EPSG:3857",
            zoom=zoom,
            interpolation="bilinear",
            reset_extent=False,
            attribution=False,
        )

        lines.plot(
            ax=ax,
            color=line_color,
            linewidth=line_width,
            alpha=0.9,
            zorder=10,
        )

        ax.set_axis_off()

        fig.savefig(
            output_pdf,
            format="pdf",
            dpi=dpi,
            bbox_inches='tight',
            pad_inches=0,
        )

        plt.close(fig)
        print(f"A0 poster opgeslagen als: {output_pdf}")
        return self

    # ------------------------------------------------------------
    # Simpele interactieve folium-kaart met de routes
    # ------------------------------------------------------------
    def export_folium_map(
        self,
        output_html="routes_map.html",
        line_color="red",
        line_weight=3,
        zoom_start=12,
        today=None,
        show_extent_info=True,
    ):
        """
        Maakt een eenvoudige interactieve HTML-kaart (folium) met alle
        routes uit self.gdf erop, plus dezelfde "Last Updated"-label
        (linksboven) als de video. Vereist dat load_data() al is
        aangeroepen. Gebruikt dezelfde tile-bron als de rest van de
        klasse: Thunderforest als er een api_key is opgegeven, anders
        de gratis OsmAnd HD-tileserver.

        show_extent_info=True (standaard) toont rechtsboven een label
        met de geconfigureerde extent (self.xmin/ymin/xmax/ymax,
        omgezet van EPSG:3857 naar lat/lon) en de opgegeven
        zoom_start. Let op: m.fit_bounds() (hieronder) laat de browser
        de uiteindelijke zoom automatisch herberekenen op basis van de
        route-data, dus zoom_start is het gevraagde startniveau, niet
        per se de zoom die je meteen te zien krijgt.
        """
        try:
            import folium
        except ImportError as e:
            raise ImportError(
                "folium is niet geïnstalleerd. Installeer het met "
                "'pip install folium' om export_folium_map() te gebruiken."
            ) from e

        if self.gdf is None:
            raise RuntimeError(
                "Roep eerst load_data() aan voordat je export_folium_map() gebruikt."
            )

        if self.gdf.empty:
            raise ValueError("De GeoDataFrame is leeg.")

        # Folium werkt in lat/lon (EPSG:4326), self.gdf staat in EPSG:3857
        gdf_4326 = self.gdf.to_crs(epsg=4326)

        # [minx, miny, maxx, maxy]
        minx, miny, maxx, maxy = gdf_4326.total_bounds
        center_lat = (miny + maxy) / 2
        center_lon = (minx + maxx) / 2

        tiles = self._get_tile_source()
        attr = "Thunderforest" if self.api_key else "OsmAnd"

        m = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=zoom_start,
            tiles=tiles,
            attr=attr,
        )

        folium.GeoJson(
            gdf_4326,
            style_function=lambda feature: {
                "color": line_color,
                "weight": line_weight,
                "opacity": 0.9,
            },
        ).add_to(m)

        # Automatisch inzoomen op de volledige extent van de routes
        m.fit_bounds([[miny, minx], [maxy, maxx]])

        # "Last Updated"-label linksboven, zelfde stijl (afgeronde,
        # lichtgrijze, halftransparante box) als add_last_updated_label()
        # bij de video.
        today = today or date.today().strftime("%Y-%m-%d")
        last_updated_html = f"""
        <div style="
            position: fixed;
            top: 10px; left: 50px;
            z-index: 9999;
            background-color: lightgrey;
            opacity: 0.85;
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 14px;
            font-family: sans-serif;
        ">
            Last Updated: {today}
        </div>
        """
        m.get_root().html.add_child(folium.Element(last_updated_html))

        if show_extent_info:
            # self.xmin/ymin/xmax/ymax staan al in EPSG:3857 (meters),
            # dus die gebruiken we direct als initiële waarde voordat
            # het JS-onderdeel hieronder 'm live overneemt.
            extent_info_html = f"""
            <div id="extent-info-box" style="
                position: fixed;
                top: 10px; right: 10px;
                z-index: 9999;
                background-color: lightgrey;
                opacity: 0.85;
                padding: 6px 12px;
                border-radius: 8px;
                font-size: 13px;
                font-family: sans-serif;
                line-height: 1.4;
                white-space: nowrap;
            ">
                Extent (EPSG:3857): {self.xmin:.1f}, {self.ymin:.1f}, {self.xmax:.1f}, {self.ymax:.1f}<br>
                Zoom: {zoom_start}
            </div>
            """
            m.get_root().html.add_child(folium.Element(extent_info_html))

            # Live bijwerken bij pannen/zoomen: leest de daadwerkelijk
            # zichtbare kaart-extent (Leaflet geeft lat/lon terug) en
            # projecteert die terug naar EPSG:3857-meters via
            # Leaflet's eigen L.CRS.EPSG3857.project() - geen aparte
            # projectie-library nodig. m.get_name() geeft de
            # JS-variabelenaam van de folium-map terug. We wachten op
            # het 'load'-event van de pagina (i.p.v. dit script direct
            # te laten draaien) zodat de kaart en tile-layer
            # gegarandeerd al bestaan, ongeacht de exacte volgorde
            # waarin folium script-secties samenvoegt; anders kan de
            # kaart zelf blanco/wit blijven.
            map_var = m.get_name()
            extent_update_js = f"""
            window.addEventListener('load', function() {{
                try {{
                    var mapObj = {map_var};
                    function updateExtentInfo() {{
                        var bounds = mapObj.getBounds();
                        var zoom = mapObj.getZoom();
                        var sw3857 = L.CRS.EPSG3857.project(bounds.getSouthWest());
                        var ne3857 = L.CRS.EPSG3857.project(bounds.getNorthEast());
                        var el = document.getElementById('extent-info-box');
                        if (el) {{
                            el.innerHTML =
                                "Extent (EPSG:3857): " + sw3857.x.toFixed(1) + ", " + sw3857.y.toFixed(1) +
                                ", " + ne3857.x.toFixed(1) + ", " + ne3857.y.toFixed(1) +
                                "<br>Zoom: " + zoom;
                        }}
                    }}
                    mapObj.on('moveend', updateExtentInfo);
                    mapObj.on('zoomend', updateExtentInfo);
                    updateExtentInfo();
                }} catch (e) {{
                    console.error("Extent-info label kon niet worden bijgewerkt:", e);
                }}
            }});
            """
            m.get_root().script.add_child(folium.Element(extent_update_js))

        m.save(output_html)
        print(f"Folium-kaart opgeslagen als: {output_html}")
        return self

    # ------------------------------------------------------------
    # Alles in één keer uitvoeren
    # ------------------------------------------------------------
    def run(self, output_video):
        """
        Ketent load_data() -> ... -> build_video(). output_video is
        verplicht, zie build_video().
        """
        (
            self.load_data()
            .setup_figure()
            .create_layers()
            .add_last_updated_label()
            .compute_frame_count()
            .render_frames()
            .build_video(output_video)
        )
        return self

#%%
os.chdir(r"C:\Users\harke007\RouteMap\Git")

# ==============================================================
# Stap 1: renderer initialiseren met alleen extent + activity_filter
# ==============================================================
# data_source laten we leeg: die wordt automatisch
# "Activities_gpx_cycling" (of "Activities_gpx_running" bij
# activity_filter="running"), en meteen aangemaakt als hij nog
# niet bestaat. api_key laten we ook leeg -> gratis OpenStreetMap
# basemap. Beide kun je hieronder alsnog overschrijven indien
# gewenst (bv. renderer.api_key = "..." voor Thunderforest).
#
# De extent hieronder is een startpunt (goed voor fietsen rondom
# Wageningen) - in stap 4 kun je 'm interactief verfijnen.
renderer = CyclingMovementRenderer(
    extent=(566922.7716, 6772346.9800, 702660.2619, 6873108.8243),
    activity_filter="running",  # of "running"
)

# ==============================================================
# Stap 2: kies ÉÉN manier om de gpx-map te (laten) vullen
# ==============================================================
# Opties: "existing_gpx" / "fit_folder" / "tredict" /
#         "strava_bulk_export" / "strava_api"
DATA_SOURCE_MODE = "strava_bulk_export"

if DATA_SOURCE_MODE == "existing_gpx":
    # De gpx-map (renderer.data_source) is al gevuld -> niets te
    # doen, load_data() hieronder leest 'm gewoon in.
    pass

elif DATA_SOURCE_MODE == "fit_folder":
    # We hebben een map met alleen .fit bestanden (bv. een bulk-
    # export vanuit Garmin) -> converteren naar .gpx in
    # renderer.data_source.
    renderer.convert_fit_folder_to_gpx(fit_dir="Activities_fit")

elif DATA_SOURCE_MODE == "tredict":
    # Vult de gpx-map aan met nieuwe activiteiten van Tredict
    # vanaf start_date. Zet je token liever in een environment
    # variable dan hardcoded in het script:
    renderer.tredict_token = os.environ.get("TREDICT_TOKEN")
    renderer.sync_from_tredict(start_date="2026-08-01")

elif DATA_SOURCE_MODE == "strava_bulk_export":
    # Verwerkt een Strava bulk-export (aangevraagd via je
    # accountinstellingen -> "Download of verwijder je account" ->
    # "Alle je activiteiten downloaden"). Pak de export folder uit en hernoem hem StravaExport
    # Verwacht activities.csv plus de map StravaExport/activities met de originele
    # .fit/.gpx/.tcx (evt. .gz-gecomprimeerde) bestanden:
    renderer.sync_from_strava_export(
        csv_path="StravaExport/activities.csv",
        activities_dir="StravaExport/activities",
    )

elif DATA_SOURCE_MODE == "strava_api":
    # Vult de gpx-map aan met nieuwe activiteiten van Strava
    # vanaf start_date, volledig via de officiële API
    # (activiteitenlijst + streams). Vereist het pakket stravalib
    # voor de OAuth-token-uitwisseling. Aan te maken via
    # https://www.strava.com/settings/api; zet ze liever in
    # environment variables dan hardcoded in het script:
    renderer.strava_client_id = os.environ.get("STRAVA_CLIENT_ID")
    renderer.strava_client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    renderer.strava_refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN")
    renderer.sync_from_strava(start_date="2026-08-01")

else:
    raise ValueError(f"Onbekende DATA_SOURCE_MODE: {DATA_SOURCE_MODE!r}")

# ==============================================================
# Stap 3: data inladen (nodig voor elke output hieronder)
# ==============================================================
renderer.load_data()

# ==============================================================
# Stap 4: extent interactief verfijnen via de folium-kaart
# ==============================================================
# Open Routes_explore.html in je browser, pan/zoom naar het
# gebied dat je wil gebruiken, en lees rechtsboven het live
# "Extent (EPSG:3857): xmin, ymin, xmax, ymax"-label af (dit
# label update automatisch bij pannen/zoomen, en is als kant-en-
# klare komma-gescheiden tuple te kopiëren-plakken).
#
# Zet UPDATE_EXTENT op False om de extent uit stap 1 te laten
# staan (bv. als je 'm al goed hebt), of op True en vul
# update_extent hieronder met de gekopieerde waardes in om 'm aan
# te passen vóór het renderen van video/poster/definitieve kaart.
renderer.export_folium_map(output_html="Routes_explore.html")

UPDATE_EXTENT = False
if UPDATE_EXTENT:

    update_extent = (596858.5, 6776563.0, 662517.9, 6825100.5)
    #update_extent = (600680.4, 6790015.9, 666416.2, 6814303.7)  # <- plak hier je gekopieerde extent
    renderer.xmin, renderer.ymin, renderer.xmax, renderer.ymax = update_extent

# ==============================================================
# Stap 5: kies welke output(s) je wil draaien - meerdere mag
# ==============================================================
# Deze gebruiken de (eventueel bijgewerkte) extent uit stap 4.
# export_folium_map() hier is de DEFINITIEVE kaart (Routes.html,
# dus een andere bestandsnaam dan de verkenner uit stap 4).
RUN_VIDEO = True
RUN_FOLIUM_MAP = True
RUN_A0_POSTER = True

if RUN_VIDEO:
    (
        renderer
        .setup_figure()
        .create_layers()
        .add_last_updated_label()
        .compute_frame_count()
        .render_frames()
        .build_video(output_video="RunningMovements.mp4")
    )

if RUN_FOLIUM_MAP:
    renderer.export_folium_map(output_html="RunningRoutes.html")

if RUN_A0_POSTER:
    renderer.export_a0_map(output_pdf="RunningRoutes6.pdf")
    