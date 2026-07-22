# Ruapehu Mountain Report Bot

Automatically fetches the daily avalanche advisory and weather forecast for
Mt. Ruapehu / Tongariro (New Zealand), synthesizes them into a single
Discord-Markdown report using the Google Gemini API, and posts it to a
Discord channel via webhook. Designed to run locally or on a daily schedule
via GitHub Actions.

## How it works

1. **Fetch** raw data from three public sources:
   - [Yr.no](https://www.yr.no) (MET Norway) locationforecast API - hourly
     temperature, wind, precipitation, and sunrise/sunset.
   - [avalanche.net.nz](https://www.avalanche.net.nz/region/tongariro) -
     Tongariro avalanche advisory, fetched from the site's own public JSON
     API (`/api/forecast/tongariro`) for clean structured data - danger by
     elevation, avalanche problems, confidence, recent activity, and a
     bundled "Mountain Weather" note (freezing level/wind, sourced from
     MetService by the advisory itself).
   - [Mountain-Forecast.com](https://www.mountain-forecast.com/peaks/Ruapehu/forecasts/2796) -
     altitude-banded forecast, fetched for all three elevation bands (top
     ~2796m, mid 2000m, base 1000m).
2. **Synthesize** the combined raw data with Gemini (`gemini-flash-latest`
   by default) into a structured report (advisory, weather, route
   suggestions, source status footer).
3. **Publish** the report to Discord via an incoming webhook, as a single
   embed. Embeds allow up to 4096 characters in the description (vs. 2000
   for a plain message), and the sidebar is colour-coded to the day's worst
   avalanche danger rating (green=Low through dark red=Extreme) so the
   danger level is visible at a glance.

Every fetcher fails soft: if a site is down, blocked, or redesigned, that
source is marked `FAILED` with an error message instead of crashing the
run, and the report footer reflects which sources were used.

> **Why no MetService?** It was evaluated and deliberately dropped. Its
> mountain-forecast page has zero server-rendered content (a pure
> client-side SPA), and its underlying `/api/v2` endpoint is routed through
> DataDome bot-detection - a clear signal they don't want automated,
> non-browser access, unlike avalanche.net.nz's open public-safety API. The
> advisory's bundled "Mountain Weather" note (see above) already surfaces
> MetService-sourced context without needing a direct integration.

## 1. Local setup

### Prerequisites

- Python 3.11+
- A Google Gemini API key
- A Discord webhook URL

### Install

```bash
git clone <this-repo-url>
cd tongariro-mountain-report

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

(A conda `environment.yml` is also included if you prefer `conda env create -f environment.yml`.)

### Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your real values (see the next two sections for how
to obtain them):

```
GEMINI_API_KEY=your_gemini_api_key_here
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your/webhook/url
```

`.env` is only used for local runs (`main.py` loads it via `python-dotenv`).
**Never commit your real `.env` file** - it's already covered by
`.gitignore`, but double-check before pushing.

### Run it

```bash
python main.py
```

On success you'll see log lines for each fetch step and a final "Report
sent successfully" message, and the report will appear in your Discord
channel.

## 2. Getting a Gemini API key

1. Go to [Google AI Studio](https://aistudio.google.com/apikey).
2. Sign in with a Google account and click **Create API key**.
3. Choose or create a Google Cloud project when prompted.
4. Copy the generated key into `GEMINI_API_KEY` in your `.env` (locally) or
   into the `GEMINI_API_KEY` GitHub secret (for Actions - see below).

Keep this key secret - anyone with it can make billed requests on your
behalf.

## 3. Creating a Discord webhook

1. Open Discord and go to the server/channel you want the report posted to.
2. Click the gear icon next to the channel name (**Edit Channel**), or
   **Server Settings**.
3. Go to **Integrations -> Webhooks -> New Webhook**.
4. Give it a name (e.g. "Mountain Report Bot") and confirm the target
   channel.
5. Click **Copy Webhook URL**.
6. Paste it into `DISCORD_WEBHOOK_URL` in your `.env` (locally) or into the
   `DISCORD_WEBHOOK_URL` GitHub secret (for Actions).

Treat the webhook URL as a secret too - anyone with it can post messages to
that channel.

## 4. Running on a schedule with GitHub Actions

The workflow at `.github/workflows/daily_report.yml` runs the script every
day and can also be triggered manually.

### Set up GitHub Secrets

In your GitHub repository:

1. Go to **Settings -> Secrets and variables -> Actions**.
2. Click **New repository secret** and add:
   - `GEMINI_API_KEY` - your Gemini API key from step 2.
   - `DISCORD_WEBHOOK_URL` - your webhook URL from step 3.

### Enable the workflow

- Push this repository (including the `.github/workflows/daily_report.yml`
  file) to GitHub. Actions is enabled by default for most repos - if it's
  disabled, go to the **Actions** tab and enable workflows.
- The schedule is `cron: '0 18 * * *'` (18:00 UTC), which is **06:00 NZST**
  during the NZ winter (NZ Standard Time, UTC+12, roughly early April to
  late September - ski season). During NZ daylight saving (NZDT, UTC+13)
  this instead fires at 07:00 local time. GitHub Actions cron always runs
  in UTC and does not shift for daylight saving, so adjust the cron
  expression manually if you need a fixed local time year-round.
- To test immediately without waiting for the schedule, go to **Actions ->
  Daily Tongariro Mountain Report -> Run workflow** (this uses the
  `workflow_dispatch` trigger).

### Monitoring

If a run fails entirely (e.g. all data sources down, or Gemini/Discord
errors), the script exits with a non-zero status so the Action run shows as
failed in GitHub, and where possible it also posts a short alert message to
Discord so the channel isn't left silently outdated.

## Project structure

```
tongariro-mountain-report/
├── .env.example              # Template for local environment variables
├── config.py                 # URLs, headers, and tunable constants
├── main.py                   # Fetchers, Gemini synthesis, Discord publisher
├── requirements.txt          # Python dependencies
├── environment.yml           # Optional conda equivalent of requirements.txt
├── README.md                 # This file
└── .github/
    └── workflows/
        └── daily_report.yml  # Scheduled + manual GitHub Actions workflow
```

## Customizing

- **Model**: set `GEMINI_MODEL` in `.env` / GitHub Actions env to override
  the default `gemini-flash-latest` (a Google-maintained alias for the
  current recommended flash-tier model - used instead of a pinned version
  like `gemini-2.5-flash` because specific versions get retired for new API
  keys over time). If you pin a specific model, verify with
  `client.models.list()` first that your key can actually access it.
- **Coordinates**: `LATITUDE` / `LONGITUDE` in `config.py` point at the
  Ruapehu summit area; adjust if you want a different reference point.
- **Report length**: `TARGET_REPORT_CHAR_LIMIT` in `config.py` controls the
  target length passed to the LLM. The report is published as a Discord
  embed, whose description field has a hard limit of 4096 characters
  (`EMBED_DESCRIPTION_CHAR_LIMIT`); the script truncates as a safety net if
  the model overshoots, always preserving the source-status/disclaimer
  footer intact rather than cutting into it.
- **User-Agent**: `YR_USER_AGENT` in `config.py` should include real contact
  info per Yr.no's terms of service - it must NOT use the placeholder
  domain `example.com`, which MET Norway's API explicitly rejects with a
  403 (confirmed by testing; any other domain works fine).