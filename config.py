"""
Central configuration for the Tongariro Mountain Report bot.

All URLs, headers, and tunable constants live here so main.py stays
focused on fetch/synthesize/publish logic.
"""

import os

# --------------------------------------------------------------------------
# Data source URLs
# --------------------------------------------------------------------------

AVALANCHE_URL = "https://www.avalanche.net.nz/region/tongariro"

# avalanche.net.nz's own front-end calls this JSON API (no auth required) to
# render the page above - it returns clean structured data (danger by
# elevation, avalanche problems, confidence, recent activity) instead of a
# client-rendered HTML shell, so we call it directly rather than scraping
# the SPA. AVALANCHE_URL above is kept as the human-readable source link
# used in the report and as a fallback HTML scrape target.
AVALANCHE_API_URL = "https://www.avalanche.net.nz/api/forecast/tongariro"

# Mt Ruapehu summit area coordinates (Dome Shelter / crater rim vicinity)
LATITUDE = -39.28
LONGITUDE = 175.56
YR_NO_API_URL = (
    f"https://api.met.no/weatherapi/locationforecast/2.0/compact"
    f"?lat={LATITUDE}&lon={LONGITUDE}"
)

# mountain-forecast.com forecast IDs for Ruapehu by elevation band, verified
# against the site's own elevation-selector links (data-elevation-group).
MOUNTAIN_FORECAST_PEAK_SLUG = "Ruapehu"
MOUNTAIN_FORECAST_ELEVATIONS = {
    "top": 2796,  # summit, ~2797m
    "mid": 2000,
    "bot": 1000,
}
MOUNTAIN_FORECAST_URL = (
    f"https://www.mountain-forecast.com/peaks/{MOUNTAIN_FORECAST_PEAK_SLUG}"
    f"/forecasts/{MOUNTAIN_FORECAST_ELEVATIONS['top']}"
)

# --------------------------------------------------------------------------
# HTTP headers
# --------------------------------------------------------------------------

# Yr.no / MET Norway requires a descriptive User-Agent with real contact
# info (they explicitly reject the placeholder domain "example.com" with a
# 403 - confirmed by testing - so this MUST be changed to a real, working
# contact before deploying). See: https://developer.yr.no/doc/TermsOfService/
YR_USER_AGENT = "TongariroReportBot/1.0 changeme@yourdomain.com"

YR_HEADERS = {
    "User-Agent": YR_USER_AGENT,
    "Accept": "application/json",
}

# A standard browser-like User-Agent for scraping HTML sites. Several
# forecast/advisory sites block or misbehave for requests without one.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "TongariroReportBot/1.0 (+changeme@yourdomain.com)"
)

SCRAPE_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-NZ,en;q=0.9",
}

# --------------------------------------------------------------------------
# Networking / behaviour tunables
# --------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = 15
YR_FORECAST_HOURS_AHEAD = 24  # how many hourly timeseries entries to summarise

# Max characters of extracted page text handed to Gemini per HTML source.
# Keeps prompt size sane and avoids feeding entire nav/footer boilerplate.
MAX_SCRAPE_TEXT_CHARS = 6000

# --------------------------------------------------------------------------
# Gemini / LLM settings
# --------------------------------------------------------------------------

# "gemini-flash-latest" is a Google-maintained alias for the current
# recommended flash-tier model. We default to it (rather than pinning e.g.
# "gemini-2.5-flash") because specific model versions get retired for new
# API keys/accounts over time - pinning one risks the workflow silently
# breaking months later with a 404. Override with GEMINI_MODEL if you want
# to pin a specific version (e.g. "gemini-2.5-flash" or "gemini-2.0-flash"),
# but verify with `client.models.list()` that your key can access it first.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

# We publish the report as a single Discord embed (not plain message
# "content"), since embeds allow far more text: a "description" field can
# hold up to 4096 characters, vs. only 2000 for a normal message. The
# report's first line becomes the embed "title" (limit 256, plain-text
# only - no Markdown rendering there) and everything after it becomes the
# "description" (limit 4096, same Markdown support as normal messages).
# Discord also enforces a combined 6000-char limit across title +
# description + fields + footer for one embed, but a single title well
# under 256 chars leaves that nowhere near binding here.
EMBED_TITLE_CHAR_LIMIT = 256
EMBED_DESCRIPTION_CHAR_LIMIT = 4096
TARGET_REPORT_CHAR_LIMIT = 3600  # target for the LLM; comfortable margin under 4096 to absorb overshoot

# --------------------------------------------------------------------------
# Environment variables (loaded via python-dotenv in main.py)
# --------------------------------------------------------------------------

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
DISCORD_WEBHOOK_URL_ENV = "DISCORD_WEBHOOK_URL"
