"""Settings for the Teatr Muzyczny Gdynia free-seat watcher."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

REPERTOIRE_URL = "https://muzyczny.org/pl/repertuar.html"
TICKETS_BASE = "https://bilety.muzyczny.org/rezerwacja/"

# Only events whose stage line contains one of these are checked.
# The section names below exist on the Duża Scena seat map.
STAGES = ("Duża Scena",)

# Section label (as printed on the seat map) -> highest row number to watch.
# Rows are numbered from 1 inside each section, counting from the stage.
WATCHED_ROWS = {
    "PARTER": 5,
    "AMFITEATR": 3,
    "LOŻA LEWA": 3,
    "LOŻA PRAWA": 3,
}

MIN_SEATS_TOGETHER = 2

# Two seats are neighbours when their left edges are at most this many SVG
# units apart (seats are 16 wide with a 2 gap, so neighbours sit 18 apart).
ADJACENT_MAX_GAP = 20

REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Failure alerts: email when a run fails (site down for good, or its format
# changed) this many runs in a row; with cron every 30 min, 3 runs = 1.5 h.
ALERT_AFTER_FAILED_RUNS = 3
# A run counts as failed when at least this share of performances can't be checked.
ALERT_FAILED_EVENTS_RATIO = 0.5
# While still failing, repeat the alert at most this often.
ALERT_REPEAT_HOURS = 24

STATE_FILE = BASE_DIR / "state.json"
ENV_FILE = BASE_DIR / ".env"
LOG_FILE = BASE_DIR / "scraper.log"
