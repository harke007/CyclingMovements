# CyclingMovementRenderer

`CyclingMovementRenderer` is één Python-klasse die het hele traject afhandelt van *ruwe activiteitendata* (Tredict, Strava, of losse `.fit`/`.gpx`/`.tcx` bestanden) naar drie soorten output:

1. een **geanimeerde video** (MP4) waarin routes zichzelf op een kaart tekenen,
2. een **interactieve HTML-kaart** (folium/Leaflet), en
3. een **grote poster-PDF** (A0) met alle routes tegelijk uitgetekend.

De klasse is bewust *bron-onafhankelijk*: je kan zelf een map met `.gpx` bestanden vullen, of de klasse laten downloaden/converteren via Tredict, de officiële Strava API, of een Strava bulk-export.

---

## Inhoudsopgave

- [Installatie](#installatie)
- [Environment variables (secrets)](#environment-variables-secrets)
- [Snel starten](#snel-starten)
- [Concepten](#concepten)
  - [`activity_filter`: fietsen vs. hardlopen](#activity_filter-fietsen-vs-hardlopen)
  - [De gpx-map (`data_source`)](#de-gpx-map-data_source)
  - [Achtergrondkaart (`api_key`)](#achtergrondkaart-api_key)
- [Databronnen vullen](#databronnen-vullen)
  - [1. Bestaande gpx-map](#1-bestaande-gpx-map)
  - [2. Map met `.fit` bestanden](#2-map-met-fit-bestanden)
  - [3. Tredict](#3-tredict)
  - [4. Strava — officiële API](#4-strava--officiële-api)
  - [5. Strava — bulk-export](#5-strava--bulk-export)
- [Output genereren](#output-genereren)
  - [Video](#video)
  - [Interactieve folium-kaart](#interactieve-folium-kaart)
  - [A0-poster (PDF)](#a0-poster-pdf)
- [Volledige workflow-voorbeeld](#volledige-workflow-voorbeeld)
- [Constructor-parameters (volledig overzicht)](#constructor-parameters-volledig-overzicht)
- [Methode-referentie](#methode-referentie)
- [Strava OAuth: refresh_token verkrijgen](#strava-oauth-refresh_token-verkrijgen)
- [Bestandsstructuur / defaults](#bestandsstructuur--defaults)
- [Dependencies](#dependencies)

---

## Installatie

```bash
pip install numpy requests pillow geopandas pandas matplotlib contextily imageio imageio-ffmpeg
pip install fitparse fit2gpx gpxpy
pip install folium        # alleen nodig voor export_folium_map()
pip install stravalib     # alleen nodig voor de Strava API-integratie
```

> `build_video()` gebruikt de ffmpeg-binary die `imageio-ffmpeg` meelevert — een losse ffmpeg-installatie is normaal niet nodig.

---

## Environment variables (secrets)

Tokens en API-secrets horen **niet** hardcoded in je script (zeker niet als je het deelt of commit naar git). De klasse leest ze daarom via `os.environ.get(...)` uit, bv.:

```python
renderer.tredict_token = os.environ.get("TREDICT_TOKEN")
```

Dit zijn de variabelen die (afhankelijk van welke databron je gebruikt) nodig kunnen zijn:

| Variabele | Nodig voor | Waar te verkrijgen |
|---|---|---|
| `TREDICT_TOKEN` | `sync_from_tredict()` / `download_tredict_activities()` | Je Tredict personal API-token |
| `STRAVA_CLIENT_ID` | `sync_from_strava()` / `download_strava_activities()` | [strava.com/settings/api](https://www.strava.com/settings/api) |
| `STRAVA_CLIENT_SECRET` | idem | idem |
| `STRAVA_REFRESH_TOKEN` | idem | Zie [Strava OAuth](#strava-oauth-refresh_token-verkrijgen) — **niet** het standaardtoken van de instellingenpagina |
| `THUNDERFOREST_TOKEN` | Achtergrond kaart | Je Thunderforest API Key, als je niet de standaard OSM basemap gebruikt |

Gebruik je alleen de methodes `sync_from_strava_export()` (de bulk-export) of `convert_fit_folder_to_gpx()`? Dan zijn deze variabelen niet nodig.

### Windows: `set_env_vars.bat`

Voor het gemak staat er een klein batch-scriptje bij (`set_env_vars.bat`) dat deze vier variabelen in één keer zet:

```bat
setx TREDICT_TOKEN "..."
setx STRAVA_CLIENT_ID "..."
setx STRAVA_CLIENT_SECRET "..."
setx STRAVA_REFRESH_TOKEN "..."
setx THUNDERFOREST_TOKEN "..."
```

Gebruik:
1. Open `set_env_vars.bat` in een teksteditor en vul je echte waardes in.
2. Dubbelklik het bestand (of run het vanuit een terminal) — `setx` zet de variabelen **permanent** op user-niveau.
3. **Sluit en heropen** je terminal / Spyder / Python-sessie: een al openstaand venster leest de nieuwe waardes niet automatisch in.

Op macOS/Linux zet je ze i.p.v. dit batch-scriptje in je shell-profiel (`~/.zshrc`, `~/.bashrc`, …):

```bash
export TREDICT_TOKEN="..."
export STRAVA_CLIENT_ID="..."
export STRAVA_CLIENT_SECRET="..."
export STRAVA_REFRESH_TOKEN="..."
export THUNDERFOREST_TOKEN "..."
```

---

## Snel starten

De simpelste route: je hebt al een map met `.gpx` bestanden.

```python
from CyclingMovements import CyclingMovementRenderer

renderer = CyclingMovementRenderer(
    extent=(566922.77, 6772346.98, 702660.26, 6873108.82),  # xmin, ymin, xmax, ymax (EPSG:3857)
    activity_filter="cycling",       # of "running"
    data_source="Activities_gpx",    # optioneel, zie hieronder
)

renderer.run(output_video="cycling_movement.mp4")   # load_data() -> setup_figure() -> ... -> build_video()
```

Dat rendert `cycling_movement.mp4` in de working directory.

---

## Concepten

### `activity_filter`: fietsen vs. hardlopen

Eén instelling (`"cycling"` of `"running"`) bepaalt **overal** in de klasse welke activiteiten worden geselecteerd:

| Onderdeel | Hoe `activity_filter` wordt toegepast |
|---|---|
| Tredict-download | `sportType` van de Tredict-API wordt 1-op-1 vergeleken met `activity_filter` |
| FIT-bestanden | Het interne FIT-sportveld wordt gecontroleerd tegen `CYCLING_FIT_SPORTS` / `RUNNING_FIT_SPORTS`, met een substring-fallback (`"cycl"` / `"run"`) |
| Strava API | `sport_type` wordt vergeleken met `STRAVA_CYCLING_TYPES` (Ride, GravelRide, MountainBikeRide, ...) of `STRAVA_RUNNING_TYPES` (Run, TrailRun,) |
| Strava bulk-export CSV | `Activiteitstype`/`Activity Type` wordt gematcht op trefwoorden in **beide talen** (NL: "Fietsrit", "Hardlopen"; EN: "Ride", "Run"; incl. varianten als "Gravelrit", "Trailrun", "Loopband") |
| Standaard gpx-map | Als je geen `data_source` opgeeft, wordt automatisch `Activities_gpx_cycling` of `Activities_gpx_running` gebruikt |

Zo lopen fiets- en hardloopdata nooit per ongeluk door elkaar.

### De gpx-map (`data_source`)

`load_data()` leest **alleen** een map met `.gpx` bestanden in (geen los `.geojson`/`.geopackage`-bestand meer). Als je `data_source` niet opgeeft:

- wordt automatisch `Activities_gpx_cycling` of `Activities_gpx_running` gekozen (op basis van `activity_filter`),
- en die map wordt **meteen aangemaakt** als hij nog niet bestaat (handig bij een eerste run).

Alle `sync_from_*()`-methoden schrijven hun resultaat standaard naar deze map (`gpx_dir` parameter valt terug op `self.data_source`).

### Achtergrondkaart (`api_key`)

`api_key` is een Thunderforest API-key. Laat 'm leeg (`""` of `None`, ook de default) en de klasse valt automatisch terug op de gratis **OsmAnd HD-tileserver** — zowel voor de video, de A0-poster, als de folium-kaart.

``` python
renderer.api_key = os.environ.get("THUNDERFOREST_TOKEN")
```


---

## Databronnen vullen

Er zijn vijf manieren om de gpx-map te vullen. Kies er één per run.

> **Aanbevolen volgorde bij een eerste keer opzetten:** begin met een historische bulk-lading — optie 2 (Garmin `.fit`-export) of optie 5 (Strava bulk-export) — en gebruik daarna pas Tredict (optie 3) om **nieuwe** activiteiten sinds die eerste run bij te vullen. Zie de toelichting bij optie 3 hieronder.

### 1. Bestaande gpx-map

Niets te doen — geef `data_source` op (of laat 'm leeg voor de automatische map) en zorg dat er al `.gpx` bestanden in staan. `load_data()` leest ze in.

### 2. Map met `.fit` bestanden

```python
renderer.convert_fit_folder_to_gpx(fit_dir="Activities_fit")
```

Converteert alle `.fit` bestanden in `fit_dir` naar `.gpx` in `renderer.data_source`, en slaat daarbij automatisch niet-`activity_filter`-activiteiten en bestanden zonder trackdata over.

Deze optie is bedoeld voor een **Garmin Connect bulk-export** van je historische data (handig als startpunt, vóórdat je Tredict gebruikt voor de aanvullingen — zie optie 3). Zo kom je aan die `.fit` bestanden:

1. Vraag je Garmin-data-export aan via [Garmin Connect → Account instellingen → Uw gegevens exporteren](https://www.garmin.com/nl-NL/account/datamanagement/exportdata/) ("Alle data exporteren"). Je krijgt een e-mail met een downloadlink zodra de export klaar is (dit kan enige tijd duren).
2. De download bestaat uit één of meerdere `.zip`-bestanden. De activiteitenbestanden zelf staan in de submap:
   ```
   DI_CONNECT\DI-Connect-Uploaded-Files
   ```
3. Garmin's export is vaak zelf al opgedeeld in meerdere delen/zip-bestanden (bv. `export_1.zip`, `export_2.zip`, …). **Pak elk deel apart uit.**
4. Kopieer de inhoud van de `DI-Connect-Uploaded-Files`-map van **elk uitgepakt deel** samen naar één map, bv. `Activities_fit` — dus alle `.fit` bestanden uit alle delen bij elkaar in dezelfde map.
5. Roep daarna `convert_fit_folder_to_gpx(fit_dir="Activities_fit")` aan zoals hierboven.

### 3. Tredict

```python
renderer.tredict_token = "..."   # of via env var TREDICT_TOKEN
renderer.sync_from_tredict(start_date="2026-08-01")
```

Haalt activiteiten op van Tredict vanaf `start_date`, downloadt de originele `.fit`/`.tcx` bestanden, en converteert ze naar `.gpx`. Losse stappen zijn ook beschikbaar: `download_tredict_activities()` en `convert_fit_folder_to_gpx()`.

> **Let op — Tredict is bedoeld om bij te vullen, niet voor de eerste (historische) lading.** Gebruik Tredict pas ná een eerste run met optie 2 (Garmin `.fit`-export) of optie 5 (Strava bulk-export), en zet `start_date` dan op de datum van je laatste sync. Zo download je alleen de nieuwe activiteiten sindsdien, in plaats van je volledige geschiedenis opnieuw via Tredict te moeten ophalen.

### 4. Strava — officiële API

```python
renderer.strava_client_id = "..."
renderer.strava_client_secret = "..."
renderer.strava_refresh_token = "..."   # zie sectie "Strava OAuth" hieronder
renderer.sync_from_strava(start_date="2026-08-01")
```

Haalt activiteiten op via `GET /athlete/activities`, en bouwt voor elke activiteit zelf een `.gpx` bestand op basis van de `activities/{id}/streams` endpoint (lat/lon, hoogte, tijd) — Strava's officiële API heeft geen directe gpx-download.

> Vereist `stravalib` voor de OAuth-token-uitwisseling.

### 5. Strava — bulk-export

```python
renderer.sync_from_strava_export(
    csv_path="StravaExport/activities.csv",
    activities_dir="StravaExport/activities",
)
```

Verwerkt een Strava bulk-export (aan te vragen via **Instellingen → Mijn account → Download of verwijder je account → Alle je activiteiten downloaden**). Leest `activities.csv` (Nederlands of Engels, robuust tegen meerdere linefeeds binnen aangehaalde velden), filtert op `activity_filter`, en zet per activiteit het bronbestand om:

| Bronformaat | Verwerking |
|---|---|
| `.fit` (evt. `.gz`) | `fit_to_gpx()` |
| `.gpx` (evt. `.gz`) | uitpakken/kopiëren |
| `.tcx` (evt. `.gz`) | `tcx_to_gpx()` (namespace-agnostische XML-parser) |

---

## Output genereren

Roep na het vullen van de gpx-map altijd eerst `renderer.load_data()` aan — alle drie de outputs hieronder hebben `self.gdf` nodig.

### Video

```python
renderer.load_data()
(
    renderer
    .setup_figure()
    .create_layers()
    .add_last_updated_label()
    .compute_frame_count()
    .render_frames()
    .build_video(output_video="cycling_movement2.mp4")
)
```

Of alles in één keer via `renderer.run(output_video="cycling_movement2.mp4")` (roept zelf ook `load_data()` aan).

**Hoe het werkt:** elke track wordt geleidelijk getekend (rode lijn) met een blauwe stip op het actuele punt; de stip verdwijnt zodra die track klaar is. Frames worden als JPG weggeschreven (via matplotlib-blitting, snel) en met ffmpeg samengevoegd tot het opgegeven `output_video`-bestand.

`output_video` is een **verplichte parameter van `build_video()`/`run()`** (geen constructor-default) — net als `output_pdf` bij `export_a0_map()` en `output_html` bij `export_folium_map()`.

Relevante constructor-parameters: `frames_dir`, `fps`, `max_duration`, `dpi`, `figsize`, `zoom`, `ffmpeg_path`.

> **dpi-tip:** de klasse forceert de Agg-backend zodat de canvas-pixelgrootte altijd exact `figsize × dpi` is, onafhankelijk van Windows-schermschaling. Verhoog `dpi` gerust voor een scherpere video.

### Interactieve folium-kaart

```python
renderer.load_data()
renderer.export_folium_map(output_html="Routes.html")
```

Genereert een standalone HTML-bestand met:
- alle routes als gekleurde lijnen op dezelfde achtergrondkaart als de video,
- een **"Last Updated"**-label linksboven,
- een **live extent/zoom-label** rechtsboven (EPSG:3857-meters, komma-gescheiden) dat automatisch meebeweegt met pannen/zoomen in de browser (via Leaflet's `moveend`/`zoomend` events en `L.CRS.EPSG3857.project()`).

Parameters: `output_html`, `line_color`, `line_weight`, `zoom_start`, `today` (override voor het label), `show_extent_info`.

> Vereist `folium` (`pip install folium`).

#### Extent interactief kiezen via het live label

Het extent-label toont het formaat `Extent (EPSG:3857): xmin, ymin, xmax, ymax` — bewust komma-gescheiden, zodat je de vier getallen direct uit de browser kan kopiëren en als Python-tuple kan plakken:

1. Genereer een verkennende kaart: `renderer.export_folium_map(output_html="Routes_explore.html")`.
2. Open dat bestand in je browser, pan/zoom naar het gebied dat je wil gebruiken.
3. Kopieer de waardes achter "Extent (EPSG:3857):" uit het label rechtsboven.
4. Plak ze in je script:
   ```python
   renderer.xmin, renderer.ymin, renderer.xmax, renderer.ymax = (600680.4, 6790015.9, 666416.2, 6814303.7)
   ```
5. Render daarna de definitieve video/poster/kaart — die gebruiken automatisch deze bijgewerkte extent.

Dit is ook precies hoe stap 4-5 van het `if __name__ == "__main__":`-blok werken, zie [Volledige workflow-voorbeeld](#volledige-workflow-voorbeeld).

### A0-poster (PDF)

```python
renderer.load_data()
renderer.export_a0_map(output_pdf="Routes.pdf")
```

Rendert alle routes **volledig getekend** (geen animatie) op een A0-formaat PDF (841 × 1189 mm, 300 dpi) met dezelfde achtergrondkaart.

Parameters: `output_pdf`, `zoom` (tile-zoomniveau, los van het video-`zoom`), `line_color`, `line_width`, `margin` (extra marge rond de extent).

---

## Volledige workflow-voorbeeld

Dit is ook precies de structuur van het `if __name__ == "__main__":`-blok onderaan het bestand:

```python
# 1) Minimale initialisatie
renderer = CyclingMovementRenderer(
    extent=(566922.7716, 6772346.9800, 702660.2619, 6873108.8243),  # startpunt
    activity_filter="cycling",
)

# 2) Kies precies één manier om de gpx-map te vullen
renderer.sync_from_tredict(start_date="2026-08-01")
# (of: sync_from_strava(...), sync_from_strava_export(...),
#      convert_fit_folder_to_gpx(...), of niets als de map al gevuld is)

# 3) Data inladen (nodig voor elke output)
renderer.load_data()

# 4) Extent interactief verfijnen: verkennende kaart bekijken, live
#    label rechtsboven aflezen, en (optioneel) de extent bijwerken
renderer.export_folium_map(output_html="Routes_explore.html")
renderer.xmin, renderer.ymin, renderer.xmax, renderer.ymax = (
    600680.4, 6790015.9, 666416.2, 6814303.7  # <- gekopieerd uit het label
)

# 5) Kies welke output(s) je wil - gebruiken nu de bijgewerkte extent
renderer.setup_figure().create_layers().add_last_updated_label() \
        .compute_frame_count().render_frames().build_video(output_video="cycling_movement2.mp4")
renderer.export_folium_map(output_html="Routes.html")   # definitieve kaart, andere naam dan de verkenner
renderer.export_a0_map(output_pdf="Routes.pdf")
```

---

## Constructor-parameters (volledig overzicht)

Alle parameters zijn **keyword-only** (op `data_source` na is `extent` de enige verplichte).

| Parameter | Default | Omschrijving |
|---|---|---|
| `extent` | *verplicht* | `(xmin, ymin, xmax, ymax)` in EPSG:3857 (meters) |
| `data_source` | `None` → `Activities_gpx_{activity_filter}` | Map met `.gpx` bestanden; wordt aangemaakt als hij niet bestaat |
| `api_key` | `""` | Thunderforest API-key; leeg = gratis OsmAnd-tileserver |
| `activity_filter` | `"cycling"` | `"cycling"` of `"running"` |
| `frames_dir` | `"frames"` | Map voor tijdelijke JPG-frames |
| `fps` | `25` | Frames per seconde |
| `max_duration` | `30` | Maximale videoduur (seconden) |
| `dpi` | `100` | Render-resolutie (pixels = `figsize × dpi`) |
| `figsize` | `(16.97, 12)` | Figuurgrootte in inches |
| `zoom` | `12` | Tile-zoomniveau voor de video-basemap |
| `ffmpeg_path` | `None` | Override voor een specifieke ffmpeg-installatie |
| `tredict_token` | `None` | Tredict personal API-token |
| `tredict_base_url` | `"https://www.tredict.com/api/oauth/v2"` | |
| `tredict_page_size` | `1000` | |
| `tredict_request_delay` | `0.25` | Seconden tussen requests |
| `tredict_max_retries` | `4` | |
| `fit_dir` | `"Activities_fit"` | Map voor gedownloade `.fit`/`.tcx` bestanden |
| `strava_client_id` | `None` | Strava API-app client ID |
| `strava_client_secret` | `None` | Strava API-app client secret |
| `strava_refresh_token` | `None` | Zie [Strava OAuth](#strava-oauth-refresh_token-verkrijgen) |
| `strava_base_url` | `"https://www.strava.com/api/v3"` | |
| `strava_page_size` | `200` | |
| `strava_request_delay` | `0.5` | |
| `strava_max_retries` | `4` | |

---

## Methode-referentie

### Data laden & renderen

| Methode | Omschrijving |
|---|---|
| `load_data()` | Leest alle `.gpx` bestanden in `data_source` in, zet om naar EPSG:3857, vult `self.lines` |
| `setup_figure()` | Maakt de matplotlib-figuur + basemap voor de video |
| `create_layers()` | Maakt lege lijn-/punt-layers per track |
| `add_last_updated_label(today=None)` | Voegt "Last Updated"-tekst toe aan de video-figuur |
| `compute_frame_count()` | Bepaalt `self.step`/`self.frames` op basis van `fps` × `max_duration` |
| `draw_frame(frame)` | Tekent één frame (intern, gebruikt door `render_frames()`) |
| `render_frames()` | Rendert en schrijft alle JPG-frames weg |
| `build_video(output_video)` | Zet de JPG-frames om naar `output_video` via ffmpeg — `output_video` is verplicht |
| `run(output_video)` | Ketent `load_data → … → build_video(output_video)` — `output_video` is verplicht |
| `export_a0_map(output_pdf, zoom, line_color, line_width, margin)` | A0-poster PDF, alle routes volledig getekend |
| `export_folium_map(output_html, line_color, line_weight, zoom_start, today, show_extent_info)` | Interactieve HTML-kaart |

### Tredict

| Methode | Omschrijving |
|---|---|
| `fetch_tredict_activities(start_date)` | Haalt de ruwe activiteitenlijst op (met paginering) |
| `download_tredict_activities(start_date, fit_dir=None)` | Download + filter op `activity_filter` |
| `sync_from_tredict(start_date, fit_dir=None, gpx_dir=None)` | Download + converteer in één stap, zet `data_source` |

### FIT / TCX conversie

| Methode | Omschrijving |
|---|---|
| `fit_to_gpx(fit_path, gpx_path, check_sport=True)` | Eén `.fit` bestand omzetten; `check_sport=False` als filtering al elders gebeurde |
| `convert_fit_folder_to_gpx(fit_dir=None, gpx_dir=None)` | Hele map converteren |
| `tcx_to_gpx(tcx_path, gpx_path)` | Eén `.tcx` bestand omzetten (namespace-agnostisch) |

### Strava — officiële API

| Methode | Omschrijving |
|---|---|
| `exchange_strava_authorization_code(client_id, client_secret, code)` *(static)* | Eenmalige OAuth-code omwisselen voor een `refresh_token` |
| `list_strava_activities(start_date, filter_by_mode=True)` | Activiteitenlijst ophalen |
| `download_strava_activities(start_date, gpx_dir=None)` | Lijst + streams → `.gpx` bestanden |
| `sync_from_strava(start_date, gpx_dir=None)` | Download + zet `data_source` |

### Strava — bulk-export

| Methode | Omschrijving |
|---|---|
| `list_strava_export_activities(csv_path)` | Leest en filtert `activities.csv` |
| `import_strava_bulk_export(csv_path, activities_dir, gpx_dir=None, work_dir=None)` | Volledige verwerking (uitpakken + converteren) |
| `sync_from_strava_export(csv_path, activities_dir, gpx_dir=None)` | Bovenstaande + zet `data_source` |

> Methoden die met een `_` beginnen (bv. `_get_tile_source`, `_matches_activity_filter_fit`) zijn interne helpers en niet bedoeld om rechtstreeks aan te roepen.

---

## Strava OAuth: `refresh_token` verkrijgen

Strava's officiële API vereist een OAuth-`refresh_token` met de juiste **scope**. Het token dat standaard op je [strava.com/settings/api](https://www.strava.com/settings/api)-pagina staat heeft **alleen** de `read`-scope — dat is *niet* genoeg voor activiteiten.

**Stap 1 — autorisatie-URL bezoeken** (vervang `YOUR_CLIENT_ID`):
```
https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost&response_type=code&approval_prompt=force&scope=activity:read_all
```
Gebruik `activity:read_all` voor ook je private activiteiten, of `activity:read` voor alleen publieke.

**Stap 2** — na inloggen/toestaan word je doorgestuurd naar een niet-bestaande `localhost`-pagina; kopieer de `code=`-waarde uit de adresbalk (tot aan de eerstvolgende `&`).

**Stap 3** — wissel de **verse** code (eenmalig, enkele minuten geldig) meteen in:
```python
CyclingMovementRenderer.exchange_strava_authorization_code(
    client_id="...", client_secret="...", code="...",
)
```
Dit print een `refresh_token` — bewaar die (bv. als `STRAVA_REFRESH_TOKEN` environment variable).

Veelvoorkomende foutmeldingen:

| Foutmelding | Oorzaak |
|---|---|
| `activity:read_permission missing` (401) | `refresh_token` mist de juiste scope → doorloop stap 1-3 opnieuw met `scope=activity:read_all` |
| `400 Bad Request` bij het inwisselen van de code | De code is al gebruikt, verlopen, of verkeerd gekopieerd → haal een verse code |
| `RefreshToken ... invalid` | Copy-paste-fout (spaties/newlines), of `client_id`/`client_secret` horen niet bij dezelfde app |

---

## Bestandsstructuur / defaults

```
project/
├── CyclingMovements.py
├── Activities_gpx_cycling/      # standaard gpx-map (activity_filter="cycling")
├── Activities_gpx_running/      # standaard gpx-map (activity_filter="running")
├── Activities_fit/              # standaard fit_dir (Tredict-downloads)
├── frames/                      # tijdelijke video-frames
├── cycling_movement2.mp4        # video-output (naam verplicht op te geven, zie build_video())
├── a0_map.pdf                   # standaard A0-poster
├── Routes_explore.html          # optioneel: verkennende folium-kaart om de extent af te lezen
└── routes_map.html              # standaard folium-kaart
```

---
## Dependencies

| Package | Waarvoor |
|---|---|
| `numpy`, `pandas` | Data-verwerking |
| `geopandas` | GPX/geometrie-verwerking |
| `matplotlib` | Video-frames & A0-poster renderen |
| `contextily` | Basemap-tiles ophalen |
| `imageio` + `imageio-ffmpeg` | Video-encoding |
| `Pillow` | JPG-frames wegschrijven |
| `requests` | Tredict- en Strava-API-calls |
| `fitparse`, `fit2gpx` | FIT → GPX conversie |
| `gpxpy` | GPX-bestanden opbouwen (Strava-streams, TCX) |
| `folium` | Interactieve HTML-kaart *(optioneel)* |
| `stravalib` | Strava OAuth-token-uitwisseling *(optioneel)* |
