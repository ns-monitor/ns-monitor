# Nightscout Monitor

A high-performance, clinical-grade analytics and visualization suite for **Nightscout** and **AndroidAPS** (oref1 / openaps) data.

Built to run alongside your self-hosted Nightscout instance, **Nightscout Monitor** provides deep clinical insights, responsive trend exploration, AGP/Patterns analysis, continuous kinetic HbA1c estimation, and automated diary event tracking with sub-second query latency over multi-year datasets.

---

## Key Features

- **Dynamic Scorecards & Key Metrics**: Real-time glucose metrics (TIR 3.9–10.0 mmol/L, Time Below Range, Time Above Range, Mean Glucose, CV%, GMI, Screen-matched IOB).
- **Interactive Trace View (`/trace`)**: High-resolution 24-hour daily timeline showing sensor glucose, basal profiles, temp basals, boluses, SMBs, carb absorption (COB), and insulin on board (IOB) matching AndroidAPS homescreen calculations.
- **Patterns & Ambulatory Glucose Profile (`/patterns`)**: 24-hour modal day percentiles (p5, p10, p25, p50, p75, p90, p95), hypo episode clusters, and comparative date ranges with instant ECharts filtering.
- **Trends & Multi-Day Exploration (`/trends`)**: Long-term metric timelines, moving averages, standard deviation corridors, and custom metric toggles over weeks, months, or years.
- **Continuous Kinetic HbA1c Engine (`/hba1c`)**: Dynamic HbA1c estimation using kinetic red-blood-cell age modeling calibrated against laboratory venous blood samples.
- **Clinical Diary & Notes (`/diary`)**: Categorized clinical logging with auto-sync from AndroidAPS treatments and manual diary entry management.
- **Automated Ingestion & Aggregation**: Periodic polling pipeline syncing CGM readings, treatments, and devicestatus with in-memory deduplication and automated 5-minute bucket aggregation (`layer2_five_minute_aggregate`).
- **Zero-Friction Migrations**: Managed by `dbmate` container startup gates—safe, additive database migrations with zero risk of data loss.

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
TIMEZONE=Australia/Perth
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

Updates must be run from this application's own checkout directory. Git uses
the hidden `.git` directory in the current folder to identify both the
repository and branch to update; it does not choose a repository merely
because you are logged in as `root`.

For example, if you originally cloned the project to `/opt/ns-monitor`:

```bash
cd /opt/ns-monitor
git status --short
git pull --ff-only
docker compose restart nightscout_daemon
docker compose ps
```

An empty `git status --short` means there are no local project changes. If it
prints files, review them before pulling. `--ff-only` prevents Git from making
an unexpected merge into locally edited project files.

After a successful pull, restart `nightscout_daemon` so its running Python
process loads the updated application files. This does not recreate the
database or change its data. `docker compose up -d --build` is needed only
when you have changed the Docker build configuration or want to rebuild the
image manually.

The Compose commands affect only this project's services—`postgres`,
`migration`, and `nightscout_daemon`. They do not pull, stop, restart, or
remove unrelated Docker containers on the same host. The persistent database
directory and your `.env` file are not overwritten by `git pull`.

`dbmate` executes any new additive migration files (`db/migrations/00000x_*.sql`)
before starting the daemon. Existing clinical data remains in the persistent
database directory.

---

## License & Privacy

- **Data Privacy**: All data is stored locally in your dedicated PostgreSQL database. No metrics or telemetries are sent to third parties.
- **Medical Disclaimer**: This application is an analytics and monitoring tool for personal data exploration and is not intended to replace medical advice or clinical diagnosis.
