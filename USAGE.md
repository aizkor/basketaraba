# Usage Guide

All commands assume the virtualenv is active:

```bash
source /path/to/venv/bin/activate
```

---

## 1. First-time setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 2. Single-group pipeline (step by step)

The three pipeline stages are independent and can be run individually.

### Stage 1 — Scrape

```bash
# Basic usage: group name in quotes, output goes to data/<slug>/
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A"

# Verbose output
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" -v

# Force refresh the HTML cache (re-download all pages)
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --force

# Choose crawler engine (default is scrapy)
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --engine requests
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --engine scrapy

# Custom output directory
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --out /tmp/data

# Adjust delay between requests (default: 0.4 s)
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --sleep 1.0

# Run both engines side-by-side and compare metrics
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --compare-engines

# Save metrics JSON
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --metrics-out metrics.json
python crawler.py "SENIOR MASCULINA 3ª-GRUPO A" --metrics-history-dir .metrics/
```

### Stage 2 — Normalize

```bash
# Reads data/<slug>/, writes data/<slug>/database.json
python stats.py data/senior-masculina-3a-grupo-a

# Custom output path
python stats.py data/senior-masculina-3a-grupo-a --out /tmp/database.json
```

### Stage 3 — Build static site

```bash
# Single group
python web/build.py data/senior-masculina-3a-grupo-a/database.json

# Multiple groups in one build
python web/build.py data/senior-masculina-3a-grupo-a/database.json \
                    data/senior-femenina-1a-grupo-unico/database.json

# All groups at once (auto-discovers data/*/database.json)
python web/build.py --all

# Custom output and source dirs
python web/build.py --all --out /tmp/dist --src web/src
```

### Serve locally

```bash
cd web/dist && python -m http.server 8000
# open http://localhost:8000
```

---

## 3. Single-group dev loop helper

`scripts/run_local_preview.py` chains all three stages and launches the server.

```bash
# Full pipeline for the default group (SENIOR MASCULINA 3ª-GRUPO A)
python scripts/run_local_preview.py

# Specify a group
python scripts/run_local_preview.py --group "JUNIOR MASCULINA 1A-GRUPO UNICO"

# Skip stages that are already up to date
python scripts/run_local_preview.py --skip-crawl          # skip scraping only
python scripts/run_local_preview.py --skip-crawl --skip-stats   # rebuild + serve only

# Force-refresh the cache
python scripts/run_local_preview.py --force

# Change port
python scripts/run_local_preview.py --port 9000

# Build without serving
python scripts/run_local_preview.py --no-serve

# Choose engine
python scripts/run_local_preview.py --engine requests
```

---

## 4. Multi-group pipeline

`scripts/run_all_groups.py` discovers all active groups from the site, runs the
full pipeline for each, and builds a combined multi-group site.

```bash
# Discover all current groups, run full pipeline, serve
python scripts/run_all_groups.py

# Skip scraping — use whatever data is already on disk
python scripts/run_all_groups.py --skip-crawl

# Rebuild site only (skip scraping + stats)
python scripts/run_all_groups.py --skip-crawl --skip-stats

# Build without starting the server
python scripts/run_all_groups.py --no-serve
python scripts/run_all_groups.py --skip-crawl --skip-stats --no-serve

# Scan the full 30-week history when discovering groups (slower, for off-season runs)
python scripts/run_all_groups.py --full-season

# Force-refresh all HTML caches
python scripts/run_all_groups.py --force

# Choose engine
python scripts/run_all_groups.py --engine requests

# Change port
python scripts/run_all_groups.py --port 9000
```

---

## 5. Discover available groups

```bash
# List all groups active in the current + previous week (fast, ~2 pages)
python crawler.py --list-groups

# Scan the full 30-week history (slow, useful off-season)
python crawler.py --list-groups --full-season
```

Output is a JSON array of group objects:

```json
[
  {"name": "SENIOR MASCULINA 3A-GRUPO A", "heading": "SEN.MAS.3A-GRUPO A", "category_id": "..."},
  ...
]
```

---

## 6. Re-scraping a finished season

When the season is over, `_find_group_id` can no longer locate the group by
scanning recent jornada pages. Supply both IDs directly to bypass the scan:

```bash
# --category-id and --group-id come from group.json (or the site URL)
python crawler.py "SENIOR MASCULINA 2A-GRUPO B" \
  --engine requests \
  --category-id 68403787734a8 \
  --group-id 6888a8711b5b9
```

To find the IDs for a group you've already scraped:

```bash
cat data/senior-masculina-2a-grupo-b/group.json
```

---

## 7. Common workflows

### Refresh data for one group and preview

```bash
python scripts/run_local_preview.py --group "JUNIOR FEMENINA 1A-GRUPO UNICO"
```

### Rebuild the site from cached data (no network)

```bash
python scripts/run_local_preview.py --skip-crawl --skip-stats
# or for all groups:
python scripts/run_all_groups.py --skip-crawl --skip-stats
```

### Full refresh (force re-download of all HTML)

```bash
python scripts/run_all_groups.py --force
```

### Build from already-committed data (CI-equivalent)

```bash
python web/build.py --all
cd web/dist && python -m http.server 8000
```

### Inspect the database without the web UI

```bash
python -c "
import json, pathlib
db = json.loads(pathlib.Path('data/senior-masculina-3a-grupo-a/database.json').read_text())
print([g['name'] for g in db['teams']])
"
```

---

## 8. Scrapy runner (alternative entrypoint)

`scraper/run.py` is a thin wrapper around the embedded Scrapy project:

```bash
# Equivalent to: python crawler.py "..." --engine scrapy
python -m scraper.run "SENIOR MASCULINA 3ª-GRUPO A"
python -m scraper.run "SENIOR MASCULINA 3ª-GRUPO A" --force
python -m scraper.run "SENIOR MASCULINA 3ª-GRUPO A" --out /tmp/data
python -m scraper.run "SENIOR MASCULINA 3ª-GRUPO A" --metrics-out metrics.json

# Or via scrapy directly (lower level)
scrapy crawl basketaraba -a group="SENIOR MASCULINA 3ª-GRUPO A" -a out=data -a force=false
```

---

## 9. Flag reference

### `crawler.py`

| Flag | Default | Description |
|---|---|---|
| `--out` | `data` | Root output directory |
| `--engine` | `scrapy` | Crawler backend: `requests` or `scrapy` |
| `--sleep` | `0.4` | Seconds between requests |
| `--force` | off | Re-download all HTML (ignore disk cache) |
| `--category-id` | auto | Pre-resolved category ID (bypasses dropdown scan) |
| `--group-id` | auto | Pre-resolved group ID (bypasses jornada scan) |
| `--heading` | auto | Raw heading text used to filter jornada HTML |
| `--list-groups` | — | Print JSON list of groups; exit |
| `--full-season` | off | With `--list-groups`: scan 30 weeks instead of 2 |
| `--compare-engines` | off | Run both engines and print side-by-side metrics |
| `--metrics-out` | — | Write metrics to this JSON file |
| `--metrics-history-dir` | — | Append timestamped metrics under this directory |
| `-v` | off | Verbose logging |

### `stats.py`

| Flag | Default | Description |
|---|---|---|
| `--out` | `<group-dir>/database.json` | Output path for the normalized database |

### `web/build.py`

| Flag | Default | Description |
|---|---|---|
| `--all` | off | Auto-discover all `data/*/database.json` files |
| `--out` | `web/dist` | Output directory for the static site |
| `--src` | `web/src` | Source directory with `index.html`, `app.js`, `styles.css` |

### `scripts/run_local_preview.py`

| Flag | Default | Description |
|---|---|---|
| `--group` | `SENIOR MASCULINA 3ª-GRUPO A` | Group to process |
| `--engine` | crawler default | Override crawler engine |
| `--force` | off | Refresh HTML cache |
| `--skip-crawl` | off | Skip stage 1 |
| `--skip-stats` | off | Skip stage 2 |
| `--skip-build` | off | Skip stage 3 |
| `--no-serve` | off | Don't start the HTTP server |
| `--port` | `8000` | Preferred server port (auto-increments if busy) |

### `scripts/run_all_groups.py`

| Flag | Default | Description |
|---|---|---|
| `--full-season` | off | Scan 30 weeks when discovering groups |
| `--force` | off | Refresh HTML cache for all groups |
| `--engine` | crawler default | Override crawler engine for all groups |
| `--skip-crawl` | off | Skip stage 1 for all groups |
| `--skip-stats` | off | Skip stage 2 for all groups |
| `--skip-build` | off | Skip stage 3 |
| `--no-serve` | off | Don't start the HTTP server |
| `--port` | `8000` | Preferred server port (auto-increments if busy) |
