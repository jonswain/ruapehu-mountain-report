"""
Tongariro / Mt. Ruapehu Daily Mountain Report Bot.

Pipeline:
  1. Fetch raw data from three public sources (Yr.no API, Avalanche NZ's
     JSON API, and Mountain-Forecast.com).
  2. Hand the combined raw data to Gemini, which synthesizes it into a single
     Discord-Markdown report.
  3. POST the report to a Discord channel via webhook, as an embed.

Design notes:
  - Every fetcher is wrapped so a single source failing (timeout, HTML
    redesign, blocked request, etc.) never crashes the run. Each fetcher
    returns a dict with at minimum a "status" key of "OK" or "FAILED".
  - avalanche.net.nz renders most of its content client-side with
    JavaScript, so its HTML fallback scraper extracts all readable body
    text rather than depending on brittle CSS selectors tied to a specific
    JS framework's markup, plus a handful of best-effort regex extractions
    for commonly-labelled fields. That fallback is only used if the site's
    own JSON API (used as the primary path) ever stops working.
  - MetService was deliberately dropped as a source: its page has zero
    server-rendered content (pure JS SPA) and its underlying API is routed
    through DataDome bot-detection, signalling they don't want automated
    non-browser access to it - unlike avalanche.net.nz's open public-safety
    API. See git history / README for details.
  - The system prompt explicitly forbids inventing numbers that aren't
    present in the source data, since this report touches avalanche safety
    information.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types

import config

try:
    from zoneinfo import ZoneInfo

    NZ_TZ = ZoneInfo("Pacific/Auckland")
except Exception:  # pragma: no cover - only if tzdata is unavailable
    NZ_TZ = timezone.utc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tongariro_report")


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------

def _now_nz() -> datetime:
    return datetime.now(NZ_TZ)


def _regex_search(pattern: str, text: str, group: int = 1, flags: int = 0) -> str | None:
    match = re.search(pattern, text, flags)
    if not match:
        return None
    return match.group(group).strip()


def _extract_main_text(html: str, max_chars: int = config.MAX_SCRAPE_TEXT_CHARS) -> str:
    """Strip boilerplate and return cleaned, readable body text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg", "form", "iframe"]):
        tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned = "\n".join(lines)
    return cleaned[:max_chars]


def _scrape_page(url: str, source_name: str) -> dict:
    try:
        resp = requests.get(url, headers=config.SCRAPE_HEADERS, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        raw_text = _extract_main_text(resp.text)
        if not raw_text:
            raise ValueError("No readable text content could be extracted from the page")
        return {"status": "OK", "source": source_name, "raw_text": raw_text}
    except Exception as exc:  # noqa: BLE001 - fetchers must never raise
        logger.warning("%s fetch failed: %s", source_name, exc)
        return {"status": "FAILED", "source": source_name, "error": str(exc)}


# --------------------------------------------------------------------------
# 1. Yr.no (MET Norway) weather API
# --------------------------------------------------------------------------

def _fetch_sun_times(date_str: str) -> dict | None:
    """Best-effort sunrise/sunset lookup. Returns None on any failure."""
    try:
        resp = requests.get(
            "https://api.met.no/weatherapi/sunrise/3.0/sun",
            headers=config.YR_HEADERS,
            params={
                "lat": config.LATITUDE,
                "lon": config.LONGITUDE,
                "date": date_str,
                "offset": "+12:00",  # NZST
            },
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        props = resp.json()["properties"]
        return {
            "sunrise": props.get("sunrise", {}).get("time"),
            "sunset": props.get("sunset", {}).get("time"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sunrise/sunset fetch failed: %s", exc)
        return None


def fetch_yr_no() -> dict:
    source_name = "Yr.no (MET Norway)"
    try:
        resp = requests.get(config.YR_NO_API_URL, headers=config.YR_HEADERS, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
        timeseries = payload["properties"]["timeseries"]

        hourly = []
        for entry in timeseries[: config.YR_FORECAST_HOURS_AHEAD]:
            details = entry.get("data", {}).get("instant", {}).get("details", {})
            next_1h = entry.get("data", {}).get("next_1_hours", {})
            hourly.append(
                {
                    "time": entry.get("time"),
                    "air_temperature_c": details.get("air_temperature"),
                    "wind_speed_ms": details.get("wind_speed"),
                    "wind_from_direction_deg": details.get("wind_from_direction"),
                    "relative_humidity_pct": details.get("relative_humidity"),
                    "cloud_area_fraction_pct": details.get("cloud_area_fraction"),
                    "symbol_code": next_1h.get("summary", {}).get("symbol_code"),
                    "precipitation_mm_next_hour": next_1h.get("details", {}).get("precipitation_amount"),
                }
            )

        today_str = _now_nz().date().isoformat()
        sun_times = _fetch_sun_times(today_str)

        return {
            "status": "OK",
            "source": source_name,
            "hourly_forecast": hourly,
            "sun_times": sun_times,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s fetch failed: %s", source_name, exc)
        return {"status": "FAILED", "source": source_name, "error": str(exc)}


# --------------------------------------------------------------------------
# 2. Avalanche NZ (avalanche.net.nz) - Tongariro region advisory
# --------------------------------------------------------------------------

_DANGER_WORDS = r"(Extreme|High|Considerable|Moderate|Low|No Rating)"

# NZ / North American avalanche danger scale, as used by avalanche.net.nz's API.
_DANGER_RATING_LABELS = {0: "No Rating", 1: "Low", 2: "Moderate", 3: "Considerable", 4: "High", 5: "Extreme"}


def _danger_rating_label(rating) -> str:
    try:
        return _DANGER_RATING_LABELS.get(int(rating), f"Unknown ({rating})")
    except (TypeError, ValueError):
        return "Unknown"


def _strip_html(html_fragment: str | None) -> str:
    if not html_fragment:
        return ""
    return " ".join(BeautifulSoup(html_fragment, "html.parser").get_text(separator=" ").split())


def _elevation_band_key(altitude_from, altitude_to) -> str:
    """
    Map an altitudeDanger entry's range onto our three report bands.

    The API's convention (confirmed by inspection) is: the High Alpine band
    has altitudeFrom>=2300 with no altitudeTo (open-ended above); the Alpine
    band has both altitudeFrom and altitudeTo set (1800-2300); the Sub
    Alpine band has ONLY altitudeFrom=1800 with no altitudeTo, meaning
    "below 1800", not "from 1800 upward" - so altitudeTo presence/absence
    matters more than the raw numbers here.
    """
    if altitude_from is not None and altitude_from >= 2300:
        return "high_alpine_gt_2300m"
    if altitude_from is not None and altitude_to is not None:
        return "alpine_1800_2300m"
    if altitude_from is not None and altitude_to is None:
        return "sub_alpine_lt_1800m"
    return "alpine_1800_2300m"


def _estimate_valid_until(created_str: str | None, valid_period_str: str | None) -> str | None:
    if not created_str or not valid_period_str:
        return None
    hours_match = re.match(r"(\d+)\s*hrs?", valid_period_str, re.IGNORECASE)
    if not hours_match:
        return None
    try:
        created_dt = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S")
        return (created_dt + timedelta(hours=int(hours_match.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _parse_avalanche_api_payload(payload: dict) -> dict:
    forecasts = payload.get("forecasts") or []
    if not forecasts:
        raise ValueError("Avalanche API response contained no forecasts")
    forecast = forecasts[0]

    danger_by_elevation = {
        "high_alpine_gt_2300m": None,
        "alpine_1800_2300m": None,
        "sub_alpine_lt_1800m": None,
    }
    for entry in forecast.get("altitudeDanger", []):
        band = _elevation_band_key(entry.get("altitudeFrom"), entry.get("altitudeTo"))
        danger_by_elevation[band] = {
            "rating": _danger_rating_label(entry.get("rating")),
            "description": entry.get("description"),
        }

    problems = []
    for danger in sorted(forecast.get("avalancheDangers", []), key=lambda d: d.get("priority_level", 99)):
        aspects_raw = danger.get("aspects", {}) or {}
        active_aspects = sorted(
            {
                aspect.upper()
                for band_aspects in aspects_raw.values()
                for aspect, is_active in band_aspects.items()
                if is_active
            }
        )
        problems.append(
            {
                "priority": danger.get("priority"),
                "problem_type": (danger.get("character") or {}).get("title"),
                "likelihood_of_5": danger.get("likelihood"),
                "size_of_5": danger.get("size"),
                "trend": danger.get("trend"),
                "time_of_day": danger.get("time"),
                "aspects_affected": active_aspects or "not specified",
                "description": _strip_html(danger.get("description")),
            }
        )

    recent_activity = None
    additional_info = {}
    for item in forecast.get("additionalInformation", []) or []:
        title = item.get("title", "")
        text = _strip_html(item.get("content"))
        if "recent avalanche activity" in title.lower():
            recent_activity = text
        elif title:
            additional_info[title] = text

    return {
        "forecaster": forecast.get("forecaster"),
        "issued": forecast.get("created"),
        "last_edited": forecast.get("lastEdited"),
        "valid_period": forecast.get("validPeriod"),
        "valid_until_estimated": _estimate_valid_until(forecast.get("created"), forecast.get("validPeriod")),
        "confidence": forecast.get("confidenceLevel"),
        "confidence_reasons": forecast.get("confidenceReasons"),
        "important_information_summary": _strip_html(forecast.get("importantInformation")),
        "danger_by_elevation": danger_by_elevation,
        "avalanche_problems": problems,
        "recent_avalanche_activity": recent_activity or "None reported",
        "additional_info": additional_info,
    }


def _parse_avalanche_text(text: str) -> dict:
    """Best-effort regex fallback, used only if the JSON API call fails."""
    parsed = {
        "updated": _regex_search(r"(Issued|Updated|Published)[:\s]+([^\n]+)", text, group=2, flags=re.IGNORECASE),
        "valid_until": _regex_search(r"Valid until[:\s]+([^\n]+)", text, flags=re.IGNORECASE),
        "confidence": _regex_search(r"Confidence[:\s]+([^\n]+)", text, flags=re.IGNORECASE),
        "high_alpine_danger": _regex_search(
            rf"High\s*Alpine[^\n]{{0,60}}?{_DANGER_WORDS}", text, group=1, flags=re.IGNORECASE
        ),
        "alpine_danger": _regex_search(
            rf"(?<!High\s)(?<!Sub\s)Alpine[^\n]{{0,60}}?{_DANGER_WORDS}", text, group=1, flags=re.IGNORECASE
        ),
        "sub_alpine_danger": _regex_search(
            rf"Sub\s*Alpine[^\n]{{0,60}}?{_DANGER_WORDS}", text, group=1, flags=re.IGNORECASE
        ),
    }
    return parsed


def fetch_avalanche_nz() -> dict:
    """
    Avalanche NZ's own front-end calls a JSON API (AVALANCHE_API_URL) to
    render the advisory page, which gives us clean structured data instead
    of scraping a client-rendered SPA shell. We call that API directly, and
    only fall back to best-effort HTML scraping if it ever stops working.
    """
    source_name = "Avalanche NZ"
    try:
        resp = requests.get(
            config.AVALANCHE_API_URL, headers=config.SCRAPE_HEADERS, timeout=config.REQUEST_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
        parsed = _parse_avalanche_api_payload(resp.json())
        return {
            "status": "OK",
            "source": source_name,
            "source_url": config.AVALANCHE_URL,
            "parsed": parsed,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s API fetch failed (%s); falling back to HTML scrape", source_name, exc)

    result = _scrape_page(config.AVALANCHE_URL, source_name)
    if result["status"] == "OK":
        result["parsed"] = _parse_avalanche_text(result["raw_text"])
        result["note"] = (
            "Fetched via HTML fallback because the JSON API call failed. "
            "This site is largely JavaScript-rendered, so 'parsed' fields "
            "are best-effort regex matches and may be empty."
        )
    return result


# --------------------------------------------------------------------------
# 3. Mountain-Forecast.com - altitude-banded forecast
# --------------------------------------------------------------------------

def _parse_mountain_forecast_tables(html: str) -> list[dict]:
    """Best-effort parse of any <table> elements into row/col dicts."""
    soup = BeautifulSoup(html, "html.parser")
    tables_data = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if any(cells):
                rows.append(cells)
        if rows:
            tables_data.append(rows)
    return tables_data


def _fetch_mountain_forecast_band(band: str, elevation_id: int) -> dict:
    url = f"https://www.mountain-forecast.com/peaks/{config.MOUNTAIN_FORECAST_PEAK_SLUG}/forecasts/{elevation_id}"
    resp = requests.get(url, headers=config.SCRAPE_HEADERS, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    tables = _parse_mountain_forecast_tables(resp.text)
    raw_text = _extract_main_text(resp.text)
    if not raw_text and not tables:
        raise ValueError("No readable text or table content could be extracted from the page")
    return {"url": url, "elevation_m": elevation_id, "tables": tables, "raw_text": raw_text}


def fetch_mountain_forecast() -> dict:
    """
    Fetches all three elevation-banded forecast pages (top/summit, mid,
    bottom) that mountain-forecast.com serves for this peak. Each band is
    fetched independently so one bad elevation page doesn't take out the
    others; overall status is OK if all three succeeded, PARTIAL if some
    did, FAILED only if all three failed.
    """
    source_name = "Mountain-Forecast"
    bands: dict[str, dict] = {}
    errors: dict[str, str] = {}

    for band, elevation_id in config.MOUNTAIN_FORECAST_ELEVATIONS.items():
        try:
            bands[band] = _fetch_mountain_forecast_band(band, elevation_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s (%s band, %sm) fetch failed: %s", source_name, band, elevation_id, exc)
            errors[band] = str(exc)

    if not bands:
        return {"status": "FAILED", "source": source_name, "error": f"All elevation bands failed: {errors}"}

    return {
        "status": "OK" if not errors else "PARTIAL",
        "source": source_name,
        "source_url": config.MOUNTAIN_FORECAST_URL,
        "bands": bands,  # keys: "top" (~2796m summit), "mid" (2000m), "bot" (1000m)
        "band_errors": errors or None,
        "note": (
            "'bands' groups data by elevation ('top'=summit ~2796m, "
            "'mid'=2000m, 'bot'=1000m). Each band's 'tables' contains raw "
            "HTML <table> rows (temperature/wind/conditions grids); "
            "'raw_text' is cleaned body text as an LLM-friendly fallback."
        ),
    }


# --------------------------------------------------------------------------
# Gemini synthesis
# --------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """\
You are an expert backcountry mountain conditions editor writing a daily
report for Mt. Ruapehu and the Tongariro region of New Zealand, for an
audience of experienced backcountry skiers, splitboarders, and mountaineers.

You will be given raw JSON data scraped/fetched moments ago from three
public sources: Yr.no (weather API), Avalanche NZ (avalanche advisory, which
also includes a bundled "Mountain Weather" note sourced from MetService),
and Mountain-Forecast.com (altitude-banded forecast). Some sources may have
status "FAILED" and contain only an error message - skip those sections
gracefully rather than inventing data.

STRICT RULES:
- Never invent or guess specific numbers (temperatures, wind speeds, danger
  ratings, likelihood/size scores, times) that are not actually present in
  the provided data. If a specific field is not present in the data for a
  source that otherwise succeeded, write "not stated" or "unclear from
  source" for that field instead of fabricating a plausible-sounding value.
- If a source's status is "FAILED", do not describe its content at all;
  just reflect that in the footer status row.
- This report will be published as a Discord embed: your first line becomes
  the embed's plain-text title (Markdown is NOT rendered there - no #, **,
  etc. will display specially), and everything after it becomes the embed
  "description", which DOES support Discord Markdown (**bold**, *italic*,
  ### headers, bullet lists, and fenced code blocks with ```).
  Discord does NOT render GitHub-style pipe tables as real tables, so put
  any tabular comparison inside a fenced ``` code block so columns stay
  aligned in a monospace font.
- The ENTIRE output (title line + everything after it) must be under
  {char_limit} characters total. Be concise. Prioritise the avalanche
  advisory and danger ratings section - trim other sections first if you
  are running long.
- Do not wrap the whole message in an outer code block - only use a code
  block for the weather comparison table.
- Do not include any preamble, meta-commentary, or explanation of what you
  are doing. Output ONLY the final report.

Produce the report with EXACTLY these sections, in this order:

1. First line, exactly (this becomes the embed title - do not add Markdown
   emphasis to it, and do not repeat it anywhere else in the report):
   `🏔️ Mt. Ruapehu / Tongariro Daily Mountain Report - {today}`

2. `### ⚠️ Avalanche Advisory`
   - Updated (date/time), Valid until, Confidence level
   - Danger rating by elevation: High Alpine (>2300m), Alpine (1800-2300m),
     Sub Alpine (<1800m)
   - Primary avalanche problem: aspects affected, likelihood (X/5), size
     (X/5), trend/timing
   - Secondary avalanche problem (if any), with the same detail
   - Recent avalanche activity notes (if any)
   - If the Avalanche NZ source failed, clearly state that the advisory
     could not be retrieved and readers must check avalanche.net.nz
     directly before travelling.

3. `### ☀️ Weather & Sun Overview`
   - Sunrise / sunset times
   - A short note on intraday trends (e.g. temperature drops, freezing
     level changes, wind increasing) based only on what the data shows
   - A compact Markdown table (inside a fenced code block) comparing
     Top (>2300m) / Mid (1800m) / Base (1600m): temperature, wind, and
     conditions, using whatever data is available across the weather
     sources. Use "n/a" for any cell you cannot support from the data.

4. `### 🎿 Route Suggestions & Trip Planning`
   - Practical guidance for parties departing from Iwikau Village (1600m)
     or the NZAC Ruapehu / Delta Ridge Hut (2080m)
   - Aspects/terrain to avoid today given the avalanche problems above
   - Red flags to watch for during the day
   - Essential gear reminder (transceiver, shovel, probe, and anything
     else relevant to today's conditions)
   - This section should be genuinely conditioned on the advisory/weather
     above - do not give generic filler advice unrelated to today's data.

5. Footer, formatted as small text using `-#` prefix or italics:
   - A one-line data source status row, e.g.
     `Sources: Avalanche NZ (OK) | Yr.no (OK) | Mountain-Forecast (OK)`
   - Then exactly this note on its own line:
     *🤖 Note: Generated by LLM synthesizing multiple public sources.
     Readers must perform further research and check official sources at
     avalanche.net.nz before making backcountry decisions.*
"""


def _source_status_line(data: dict) -> str:
    labels = {
        "avalanche_nz": "Avalanche NZ",
        "yr_no": "Yr.no",
        "mountain_forecast": "Mountain-Forecast",
    }
    parts = []
    for key, label in labels.items():
        status = data.get(key, {}).get("status", "FAILED")
        parts.append(f"{label} ({status})")
    return "Sources: " + " | ".join(parts)


def synthesize_report(data: dict) -> str:
    api_key = _require_env(config.GEMINI_API_KEY_ENV)
    client = genai.Client(api_key=api_key)

    today_str = _now_nz().strftime("%A %d %B %Y")
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        today=today_str, char_limit=config.TARGET_REPORT_CHAR_LIMIT
    )

    # Give the model an authoritative, pre-computed status line so it
    # doesn't have to infer FAILED/OK wording itself.
    status_line = _source_status_line(data)
    user_content = (
        f"Precomputed footer status line (use this verbatim in the footer):\n{status_line}\n\n"
        f"Raw source data as JSON:\n{json.dumps(data, indent=2, default=str)}"
    )

    logger.info(
        "Requesting report synthesis from Gemini (model=%s, max_output_tokens=%d, thinking_budget=%d)",
        config.GEMINI_MODEL, config.GEMINI_MAX_OUTPUT_TOKENS, config.GEMINI_THINKING_BUDGET,
    )
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.4,
            max_output_tokens=config.GEMINI_MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_budget=config.GEMINI_THINKING_BUDGET),
        ),
    )

    finish_reason = None
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        finish_reason = getattr(candidates[0], "finish_reason", None)

    usage = getattr(response, "usage_metadata", None)
    logger.info(
        "Gemini response received: finish_reason=%s, prompt_tokens=%s, thoughts_tokens=%s, "
        "output_tokens=%s, total_tokens=%s",
        finish_reason,
        getattr(usage, "prompt_token_count", None),
        getattr(usage, "thoughts_token_count", None),
        getattr(usage, "candidates_token_count", None),
        getattr(usage, "total_token_count", None),
    )
    if finish_reason is not None and str(finish_reason) != "FinishReason.STOP":
        logger.warning(
            "Gemini finished with reason %s instead of STOP - the report may be truncated or incomplete",
            finish_reason,
        )

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response")

    logger.info("Gemini report text length: %d chars", len(text))
    return text


def _split_into_chunks(text: str, limit: int) -> list[str]:
    """
    Split text into <=limit-char chunks for posting as multiple Discord
    messages, breaking at clean paragraph/line boundaries where possible so
    no chunk cuts off mid-sentence. Used instead of hard-truncating, so a
    report that's genuinely too long for one embed still reaches readers in
    full across multiple posts.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = window.rfind("\n\n")
        if split_at < limit // 2:  # no good paragraph break; fall back to a line break
            split_at = window.rfind("\n")
        if split_at < limit // 2:  # still nothing reasonable; hard split
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _split_title_and_body(report_text: str) -> tuple[str, str]:
    """First line -> embed title (plain text); the rest -> embed description."""
    first_line, _, rest = report_text.partition("\n")
    title = first_line.lstrip("#").strip()
    return title, rest.strip()


# NZ / North American avalanche danger scale colours, worst-to-best, used
# for the embed's sidebar colour so the danger level is visible at a glance
# even before reading the text.
_DANGER_COLORS = {
    "Extreme": 0x8B0000,
    "High": 0xED4245,
    "Considerable": 0xE67E22,
    "Moderate": 0xFEE75C,
    "Low": 0x43B581,
    "No Rating": 0x99AAB5,
}
_DEFAULT_EMBED_COLOR = 0x3498DB  # neutral blue, used if avalanche data is unavailable


def _embed_color_for(data: dict) -> int:
    """Colour the embed by the worst danger rating present across elevation bands."""
    avalanche = data.get("avalanche_nz", {})
    if avalanche.get("status") != "OK":
        return _DEFAULT_EMBED_COLOR

    danger_by_elevation = (avalanche.get("parsed") or {}).get("danger_by_elevation") or {}
    ratings_present = {
        band["rating"] for band in danger_by_elevation.values() if band and band.get("rating")
    }
    for rating in _DANGER_COLORS:  # dict insertion order = worst to best
        if rating in ratings_present:
            return _DANGER_COLORS[rating]
    return _DEFAULT_EMBED_COLOR


def build_report_embeds(report_text: str, data: dict) -> list[dict]:
    """
    Turn the LLM's Markdown report into one or more Discord embeds. Embeds
    allow up to 4096 characters in the description field (vs. 2000 for a
    plain message), so a single embed fits the whole report on a typical
    day. If the body still exceeds that limit, split it across multiple
    embeds (sent as separate posts) rather than truncating, so every part
    of the report - including the safety-critical footer - always reaches
    readers.
    """
    title, body = _split_title_and_body(report_text)
    logger.info("Report body length: %d chars (embed limit %d)", len(body), config.EMBED_DESCRIPTION_CHAR_LIMIT)

    chunks = _split_into_chunks(body, config.EMBED_DESCRIPTION_CHAR_LIMIT)
    if len(chunks) > 1:
        logger.warning(
            "Report body exceeded embed description limit; splitting into %d posts", len(chunks)
        )

    color = _embed_color_for(data)
    timestamp = datetime.now(timezone.utc).isoformat()
    embeds = []
    for i, chunk in enumerate(chunks):
        chunk_title = title if i == 0 else f"{title} (continued {i + 1}/{len(chunks)})"
        embeds.append(
            {
                "title": chunk_title[: config.EMBED_TITLE_CHAR_LIMIT],
                "description": chunk,
                "color": color,
                "timestamp": timestamp,
            }
        )
    return embeds


# --------------------------------------------------------------------------
# Discord publishing
# --------------------------------------------------------------------------

def send_report_embeds(embeds: list[dict]) -> None:
    """
    Post each embed as its own Discord message. Sent as separate requests
    (rather than bundled into one message's "embeds" array) because Discord
    caps the combined title+description+footer length across all embeds in
    a single message at 6000 chars - two near-4096-char embeds would blow
    past that if bundled together.
    """
    webhook_url = _require_env(config.DISCORD_WEBHOOK_URL_ENV)
    for i, embed in enumerate(embeds):
        logger.info(
            "Posting report part %d/%d to Discord (%d chars)", i + 1, len(embeds), len(embed["description"])
        )
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()


def send_alert(message: str) -> None:
    """Plain-content message, used only for the bot's own failure alerts."""
    webhook_url = _require_env(config.DISCORD_WEBHOOK_URL_ENV)
    resp = requests.post(webhook_url, json={"content": message}, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable '{name}' is not set")
    return value


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def gather_data() -> dict:
    logger.info("Fetching Yr.no forecast...")
    yr_no = fetch_yr_no()
    logger.info("Fetching Avalanche NZ advisory...")
    avalanche_nz = fetch_avalanche_nz()
    logger.info("Fetching Mountain-Forecast.com...")
    mountain_forecast = fetch_mountain_forecast()

    data = {
        "report_date_nz": _now_nz().date().isoformat(),
        "yr_no": yr_no,
        "avalanche_nz": avalanche_nz,
        "mountain_forecast": mountain_forecast,
    }

    for key, result in data.items():
        if not isinstance(result, dict) or "status" not in result:
            continue
        status = result.get("status")
        if status == "FAILED":
            logger.warning("Source '%s' failed: %s", key, result.get("error"))
        else:
            logger.info("Source '%s' status: %s", key, status)

    return data


def main() -> int:
    load_dotenv()
    logger.info("Starting Tongariro Mountain Report run for %s", _now_nz().isoformat())

    data = gather_data()

    all_failed = all(
        isinstance(v, dict) and v.get("status") == "FAILED"
        for k, v in data.items()
        if k != "report_date_nz"
    )
    if all_failed:
        logger.error("All data sources failed; sending an alert instead of a full report")
        try:
            send_alert(
                "⚠️ **Tongariro Mountain Report failed to generate today.**\n"
                "All upstream data sources (Yr.no, Avalanche NZ, "
                "Mountain-Forecast) failed to fetch. Please check "
                "avalanche.net.nz directly, and check the bot logs.\n"
                "-# 🤖 Automated alert from the mountain report bot."
            )
        except Exception:
            logger.exception("Failed to send failure alert to Discord")
        return 1

    try:
        report = synthesize_report(data)
        embeds = build_report_embeds(report, data)
    except Exception:
        logger.exception("Gemini synthesis failed")
        try:
            send_alert(
                "⚠️ **Tongariro Mountain Report failed to generate today.**\n"
                "Data was fetched but report synthesis failed. Please check "
                "avalanche.net.nz directly, and check the bot logs.\n"
                "-# 🤖 Automated alert from the mountain report bot."
            )
        except Exception:
            logger.exception("Failed to send failure alert to Discord")
        return 1

    try:
        send_report_embeds(embeds)
    except Exception:
        logger.exception("Discord publish failed")
        return 1

    total_chars = sum(len(e["description"]) for e in embeds)
    logger.info("Report sent successfully (%d parts, %d total chars)", len(embeds), total_chars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
