# Nightscout Monitor

A self-hosted analytics and visualisation suite for **Nightscout** and **AndroidAPS** (oref1 / openaps), with dashboards, clinical reports, trend exploration, and diary logging. Compatibility with other data sources may vary.

**NS Monitor** provides deep clinical insights, responsive trend exploration, AGP/Patterns analysis, continuous HbA1c estimation, and diary event tracking. It is designed to show graphs with sub-second query latency over multi-year datasets.


---

## Key Features

- The dashboard provides an at-a-glance view of how you are going, with around 30 configurable widgets to choose from and a drag and drop layout to position each widget. Up to 6 dashboards can be adjusted for different screen sizes (computer, tablet, phone) and saved as part of the application configuration.
-  **EChart Graphs**: Many of the graphs allow you to show whatever combination of available metrics you choose. You can format how you want to display it – colour and opacity, graph type, smoothing and Y-Axis scale.
- **Trace (`/trace`)**: High-resolution 24-hour daily timeline showing any combination of Basal Profile, Basal Rate, BG, BGI, Bolus, Carbs, COB, DEV-BGI, Deviation, IOB, ISF. Useful when investigating what happened on a particular day or around a particular event.
- **Patterns (`/patterns`)**: shows 24 hour modal overlays such as the standard Ambulatory Glucose Profile and Time in Range heat map. There are numerous other metrics to choose from - AGP, Basal Profile, COB (gms), Controller Effort (%), CV (%), Dynamic ISF, HBGI, Hyper AUC (>7.8), Hypo AUC (<3.9), IOB (U), LBGI, Low Event Count / hour, Median BG, Median BGI, Median Deviation, TIR (3.9-10), TIR Heat, TITR (3.9-7.8), Warning Event Count / hour. Use it to look for time-of-day patterns rather than individual days.
- **Trends (`/trends`)**: Trends is for longer-term changes across weeks, months or years. It charts selected measures over time and supports comparisons, moving and monthly averages. Available metrics for display are Avg BG, Carbs (g), CV (%), GMI (%), GVI, HBGI, LBGI, TDD, TDD / Weight, TIR (%), TITR (%). This is the place to review whether a change in treatment, routine or settings has had a sustained effect.
- **Calendar**: The calendar view gives a month-by-month overview of glucose measures and daily summaries. It makes it easier to spot unusually good or difficult days, gaps in data, and repeated patterns across a longer period. A wide range of filters and overlays can be constructed to adjust what is displayed on the calendar.
- **Continuous HbA1c Engine (`/hba1c`)**: Dynamic HbA1c estimation using red-blood-cell age modeling calibrated against your own laboratory venous blood samples.
- **Clinical Diary & Notes (`/diary`)**: Categorized clinical logging with auto-sync from AndroidAPS treatments and manual diary entry management.
- **Automated Ingestion & Aggregation**: Periodic polling pipeline syncing CGM readings, treatments, and devicestatus with in-memory deduplication and automated 5-minute aggregation.

---

## Architecture & Technology Stack

```
┌─────────────────────────────────────────────────────────────┐
│                    Browser Client                           │
│  (Vanilla JS + ECharts + CSS Slate Dark Theme / Linear-UI)  │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP / WebSocket (Port 8080)
┌──────────────────────────────▼──────────────────────────────┐
│                  nightscout_daemon                          │
│     (Flask 3.0, psycopg2, background poller)                 │
└──────────────┬──────────────────────────────▲───────────────┘
               │                              │
               ▼                              │ Polls every 5m
┌──────────────────────────────┐  ┌───────────┴──────────────┐
│        nightscout_db         │  │   Upstream Nightscout    │
│  (PostgreSQL 15 + matviews)  │  │     (REST API v1)        │
└──────────────▲───────────────┘  └──────────────────────────┘
               │
┌──────────────┴───────────────┐
│     nightscout_migration     │
│   (amacneil/dbmate:2 runner) │
└──────────────────────────────┘
```

---

## Quickstart & Installation

### Prerequisites

- A 64-bit Ubuntu host with an account that can use `sudo`.
- Git, for cloning the project and receiving updates.
- Docker Engine and the Docker Compose plugin.
- A running Nightscout instance with API access.

#### Install Git and Docker on Ubuntu

On a new Ubuntu host, install Git and Docker's current Engine and Compose
plugin from Docker's official package repository:

```bash
sudo apt update
sudo apt install -y ca-certificates curl git

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker

git --version
docker --version
docker compose version
sudo docker run hello-world
```

These commands install the Docker Compose plugin used throughout this guide.
Docker publishes its current Ubuntu installation instructions at
https://docs.docker.com/engine/install/ubuntu/.

### 1. Clone the Repository
```bash
git clone https://github.com/ns-monitor/ns-monitor.git
cd ns-monitor
```

### 2. Configure Environment
Copy the configuration template:
```bash
cp .env.example .env
```
Edit `.env` and set your credentials:
```ini
# PostgreSQL permanent password (set before first run)
POSTGRES_PASSWORD=choose_a_strong_password

# Host directory for persistent database files (defaults to /var/lib/nightscout-monitor/postgres)
POSTGRES_DATA_DIR=/var/lib/nightscout-monitor/postgres

# Your Nightscout instance URL and API secret
NIGHTSCOUT_URL=https://your-nightscout.example.com/api/v1
API_SECRET=your_nightscout_api_secret

# Your local timezone
TIMEZONE=UTC
```

### 3. Launch Services
```bash
docker compose up -d --build
docker compose ps
```
The startup sequence automatically:
1. Boots the `postgres` database container.
2. Waits for PostgreSQL health check (`pg_isready`).
3. Runs the `migration` container to apply all ledger SQL files via `dbmate`.
4. Starts the `nightscout_daemon` web application on port **8080**.

Open your browser to:
```
http://localhost:8080
```

---

## Upgrades & Maintenance

To update to the latest release:
```bash
cd /opt/ns-monitor
git pull
docker compose up -d --build
```

---

## License & Privacy

- **Data Privacy**: All data is stored locally in your dedicated PostgreSQL database. No metrics or telemetries are sent to third parties.
- **Medical Disclaimer**: This application is an analytics and monitoring tool for personal data exploration and is not intended to replace medical advice or clinical diagnosis.
