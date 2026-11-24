"""Watch Teatr Muzyczny Gdynia for free seats near the stage and email about new ones.

Usage:
    python scraper.py            # check, email new finds, remember them
    python scraper.py --dry-run  # check and print, no email, no state change
"""

from __future__ import annotations

import argparse
import html as htmllib
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config
import notifier

log = logging.getLogger("muzyczny")

FREE_CLASS = "w"  # site CSS: w = free (white); x/z = unavailable; n = in your own cart
SECTION_LABEL_RE = re.compile(r"^[A-ZĄĆĘŁŃÓŚŹŻ][A-ZĄĆĘŁŃÓŚŹŻ ]{3,}$")
ROMAN_RE = re.compile(r"^[IVXLC]+$")
SEAT_TITLE_RE = re.compile(r"Rząd:\s*([^;<]+?)\s*;\s*Miejsce:\s*([^;<]+?)\s*(?:<|;|$)")
PRICE_RE = re.compile(r"<td>([^<]+)</td>\s*<td>([^<]+)</td>")
JS_VAR_RE = r"\b{}\s*=\s*'?([0-9a-f]+)'?\s*;"


@dataclass
class Event:
    id: str
    ident: str
    title: str
    date: date
    time: str
    stage: str
    ticket_url: str


@dataclass
class Seat:
    id: int
    row: int
    number: int
    price: str
    free: bool
    x: float = 0.0
    y: float = 0.0
    section: str | None = None


@dataclass
class SeatGroup:
    section: str
    row: int
    seats: list[Seat] = field(default_factory=list)


# ---------------------------------------------------------------- parsing

def parse_repertoire(page_html: str) -> list[Event]:
    """Events with numbered-seat ticket links on the watched stages."""
    soup = BeautifulSoup(page_html, "html.parser")
    events = []
    for row in soup.select("div.kalendarium_row"):
        link = row.find("a", href=re.compile(r"rezerwacja/numerowane\.html"))
        if not link:
            continue
        details = row.select_one(".spektakl_szczegoly")
        if not details:
            continue
        title_el = details.select_one(".h2")
        info_el = details.select_one(".h6")
        title = title_el.get_text(" ", strip=True) if title_el else "?"
        info = [p.strip() for p in (info_el.get_text(" ", strip=True) if info_el else "").split("/")]
        when = info[0] if info else ""
        stage = info[1] if len(info) > 1 else ""
        if config.STAGES and not any(s.lower() in stage.lower() for s in config.STAGES):
            continue

        url = urljoin(config.TICKETS_BASE, link["href"])
        qs = parse_qs(urlparse(url).query)
        try:
            ev_date = date(int(row["data-year"]), int(row["data-month"]), int(row["data-day"]))
        except (KeyError, ValueError):
            continue
        events.append(Event(
            id=qs.get("id", [""])[0],
            ident=qs.get("identyfikator", [""])[0],
            title=title,
            date=ev_date,
            time=when,
            stage=stage,
            ticket_url=url,
        ))

    # The calendar can list the same performance more than once.
    unique = {e.id: e for e in events if e.id}
    return sorted(unique.values(), key=lambda e: (e.date, e.time))


def parse_seat_page(page_html: str):
    """Return (request params for miejsca-wolne, {seat_id: (x, y)}, [(label, x, y)])."""
    params = {}
    for name in ("wiz_id", "wiz_idt", "ter_id", "ter_idt", "cen_id"):
        m = re.search(JS_VAR_RE.format(name), page_html)
        if not m:
            raise ValueError(f"seat page has no {name}")
        params[name] = m.group(1)

    # The seat map SVG is embedded as a string inside an inline <script>,
    # so an HTML parser would skip it; cut it out and parse it on its own.
    start, end = page_html.find("<svg"), page_html.rfind("</svg>")
    if start == -1 or end == -1:
        raise ValueError("seat page has no seat map")
    svg = page_html[start:end + len("</svg>")].replace('\\"', '"').replace("\\/", "/")
    soup = BeautifulSoup(svg, "html.parser")
    positions = {}
    for el in soup.find_all(class_="miejsce"):
        seat_id = el.get("id", "")
        if not seat_id.isdigit() or el.get("x") is None or el.get("y") is None:
            continue
        positions[int(seat_id)] = (float(el["x"]), float(el["y"]))

    labels = []
    for t in soup.find_all("text"):
        name = " ".join(t.get_text(" ", strip=True).split())
        if not SECTION_LABEL_RE.match(name) or ROMAN_RE.match(name):
            continue
        holder = t if t.get("x") is not None else t.find("tspan", x=True)
        if holder is None or holder.get("y") is None:
            continue
        try:
            labels.append((name, float(holder["x"]), float(holder["y"])))
        except ValueError:
            continue
    return params, positions, labels


def _to_int(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    roman = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total = 0
    for i, ch in enumerate(value):
        v = roman[ch]
        total += -v if i + 1 < len(value) and roman[value[i + 1]] > v else v
    return total


def parse_free_json(data: dict) -> list[Seat]:
    if data.get("status") not in (0, "0"):
        raise ValueError(f"miejsca-wolne error: {data.get('komunikat')!r}")
    seats = []
    for item in data.get("miejsca", []):
        title = htmllib.unescape(item.get("title") or "").replace("\xa0", " ")
        m = SEAT_TITLE_RE.search(title)
        if not m:
            continue
        try:
            row, number = _to_int(m.group(1)), _to_int(m.group(2))
        except (KeyError, ValueError):
            continue
        prices = [f"{a.strip()} {b.strip()}" for a, b in PRICE_RE.findall(title)]
        # Only the full-price ticket ("bilet normalny", "NORMALNY", "bilet normalny I");
        # fall back to everything if a show names it differently.
        normal = [p for p in prices if "normalny" in p.lower()]
        price = ", ".join(normal or prices)
        seats.append(Seat(
            id=int(item["id"]),
            row=row,
            number=number,
            price=price,
            # e.g. "w w3": free, in price tier 3
            free=FREE_CLASS in (item.get("class") or "").split(),
        ))
    if data.get("miejsca") and not seats:
        raise ValueError("seat descriptions no longer look like 'Rząd: N;Miejsce: N'")
    return seats


# ------------------------------------------------------------- seat logic

def assign_sections(positions: dict, labels: list) -> dict[int, str]:
    """Map seat id -> section label using where the seat sits on the map.

    Labels split the map into horizontal bands (a band runs from a label's
    height down to the next label below). Inside a band, each row is cut into
    blocks at aisles, and a block belongs to the label nearest to it
    horizontally (e.g. LOŻA LEWA | AMFITEATR | LOŻA PRAWA).
    """
    if not labels:
        return {}
    levels: list[list] = []
    for lab in sorted(labels, key=lambda l: l[2]):
        if levels and lab[2] - levels[-1][0][2] <= 10:
            levels[-1].append(lab)
        else:
            levels.append([lab])

    def band_of(y):
        found = None
        for level in levels:
            if level[0][2] <= y:
                found = level
        return found

    rows: dict[float, list] = {}
    for seat_id, (x, y) in positions.items():
        rows.setdefault(y, []).append((x, seat_id))

    sections = {}
    for y, seats in rows.items():
        level = band_of(y)
        if level is None:
            continue
        seats.sort()
        blocks, current = [], [seats[0]]
        for prev, cur in zip(seats, seats[1:]):
            if cur[0] - prev[0] > config.ADJACENT_MAX_GAP:
                blocks.append(current)
                current = []
            current.append(cur)
        blocks.append(current)
        for block in blocks:
            center = (block[0][0] + block[-1][0]) / 2
            name = min(level, key=lambda lab: abs(lab[1] - center))[0]
            for _, seat_id in block:
                sections[seat_id] = name
    return sections


def find_groups(seats: list[Seat], positions: dict, sections: dict) -> list[SeatGroup]:
    """Runs of >= MIN_SEATS_TOGETHER neighbouring free seats in watched rows."""
    by_row: dict[tuple, list[Seat]] = {}
    for s in seats:
        if not s.free or s.id not in positions:
            continue
        s.x, s.y = positions[s.id]
        s.section = sections.get(s.id)
        limit = config.WATCHED_ROWS.get(s.section or "")
        if limit is None or s.row > limit:
            continue
        by_row.setdefault((s.section, s.row, s.y), []).append(s)

    groups = []
    for (section, row, _), row_seats in sorted(by_row.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        row_seats.sort(key=lambda s: s.x)
        run = [row_seats[0]]
        for prev, cur in zip(row_seats, row_seats[1:]):
            if cur.x - prev.x <= config.ADJACENT_MAX_GAP:
                run.append(cur)
            else:
                if len(run) >= config.MIN_SEATS_TOGETHER:
                    groups.append(SeatGroup(section, row, run))
                run = [cur]
        if len(run) >= config.MIN_SEATS_TOGETHER:
            groups.append(SeatGroup(section, row, run))
    for g in groups:
        g.seats.sort(key=lambda s: s.number)
    return groups


# ------------------------------------------------------------------ state

def load_state() -> dict:
    try:
        return json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"notified": {}}
    except json.JSONDecodeError:
        log.error("state.json is corrupt; starting fresh")
        return {"notified": {}}


def save_state(state: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=config.STATE_FILE.parent, prefix=".state-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, config.STATE_FILE)


def prune_state(state: dict, today: date) -> None:
    """Forget events that have already happened."""
    notified = state.setdefault("notified", {})
    for event_id in list(notified):
        if notified[event_id].get("date", "9999-12-31") < today.isoformat():
            del notified[event_id]


def new_groups(state: dict, event: Event, groups: list[SeatGroup]) -> list[SeatGroup]:
    seen = set(state.get("notified", {}).get(event.id, {}).get("seats", []))
    return [g for g in groups if any(s.id not in seen for s in g.seats)]


def remember(state: dict, event: Event, groups: list[SeatGroup]) -> None:
    entry = state.setdefault("notified", {}).setdefault(
        event.id, {"title": event.title, "date": event.date.isoformat(), "seats": []}
    )
    entry["seats"] = sorted(set(entry["seats"]) | {s.id for g in groups for s in g.seats})


# -------------------------------------------------------------------- I/O

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    s.headers["Accept-Language"] = "pl,en;q=0.8"
    return s


def get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    for attempt in range(3):
        try:
            r = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            if attempt == 2:
                raise
            log.warning("retrying %s after %s", url, exc)
            time.sleep(5 * (attempt + 1))
    raise AssertionError("unreachable")


def check_event(session: requests.Session, event: Event) -> list[SeatGroup]:
    page = get(session, event.ticket_url).text
    params, positions, labels = parse_seat_page(page)
    data = get(
        session,
        urljoin(config.TICKETS_BASE, "miejsca-wolne.html"),
        params=params,
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": event.ticket_url},
    ).json()
    seats = parse_free_json(data)
    if not positions:
        raise ValueError("seat map has no seats")
    sections = assign_sections(positions, labels)
    if not set(config.WATCHED_ROWS) & set(sections.values()):
        raise ValueError(f"seat map has none of the watched sections (labels found: {[l[0] for l in labels]})")
    return find_groups(seats, positions, sections)


def scan(session: requests.Session, state: dict, today: date):
    """Check every upcoming event. Returns (findings, problem or None)."""
    try:
        events = parse_repertoire(get(session, config.REPERTOIRE_URL).text)
    except Exception as exc:
        return [], f"Could not read the repertoire page {config.REPERTOIRE_URL}: {exc}"
    events = [e for e in events if e.date >= today]
    if not events:
        return [], (f"Found no {', '.join(config.STAGES)} performances with a 'Kup bilet' link on "
                    f"{config.REPERTOIRE_URL}. The page layout has probably changed.")
    log.info("checking %d events", len(events))

    findings, errors = [], []
    for i, event in enumerate(events):
        if i:
            time.sleep(config.REQUEST_DELAY_SECONDS)
        try:
            groups = check_event(session, event)
        except Exception as exc:  # one broken event must not stop the run
            errors.append(f"{event.date} {event.time} {event.title}: {exc}\n    {event.ticket_url}")
            log.error("%s %s (id %s): %s", event.date, event.title, event.id, exc)
            continue
        fresh = new_groups(state, event, groups)
        log.info("%s %s %s: %d group(s), %d new", event.date, event.time, event.title, len(groups), len(fresh))
        if fresh:
            findings.append((event, fresh))

    problem = None
    if errors and len(errors) >= config.ALERT_FAILED_EVENTS_RATIO * len(events):
        shown = "\n".join(errors[:10]) + (f"\n... and {len(errors) - 10} more" if len(errors) > 10 else "")
        problem = f"{len(errors)} of {len(events)} performances could not be checked:\n\n{shown}"
    return findings, problem


def health_message(state: dict, problem: str | None, now: datetime):
    """Update the run-health record and return (subject, text) to email, or None.

    An alert goes out once the problem has lasted ALERT_AFTER_FAILED_RUNS runs in a
    row (so a short outage stays quiet), then at most every ALERT_REPEAT_HOURS while
    it lasts. A single "working again" email follows once a run succeeds.
    """
    h = state.setdefault("health", {})
    if problem is None:
        h["failed_runs"] = 0
        h["last_ok"] = now.isoformat(timespec="minutes")
        if h.pop("alert_sent", None):
            h.pop("problem", None)
            return ("Teatr Muzyczny: scraper działa ponownie",
                    "The free-seat scraper is working again; seat alerts have resumed.")
        h.pop("problem", None)
        return None

    h["failed_runs"] = h.get("failed_runs", 0) + 1
    h["problem"] = problem
    if h["failed_runs"] < config.ALERT_AFTER_FAILED_RUNS:
        return None
    last = h.get("alert_sent")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=config.ALERT_REPEAT_HOURS):
        return None
    h["alert_sent"] = now.isoformat(timespec="minutes")
    text = (
        "The Teatr Muzyczny free-seat scraper is failing, so you will not get seat alerts "
        "until it is fixed. The website's format may have changed.\n\n"
        f"Failed runs in a row: {h['failed_runs']}\n"
        f"Last successful run: {h.get('last_ok', 'never')}\n\n"
        f"{problem}\n\n"
        f"Details are in {config.LOG_FILE} on the machine running it."
    )
    return "Teatr Muzyczny: scraper nie działa", text


def run(dry_run: bool) -> int:
    session = make_session()
    today, now = date.today(), datetime.now()
    state = load_state()
    prune_state(state, today)

    findings, problem = scan(session, state, today)
    if problem:
        log.error("run problem: %s", problem.splitlines()[0])

    if findings:
        subject, text, html = notifier.build_message(findings)
        if dry_run:
            print(subject + "\n\n" + text)
        else:
            notifier.send_email(subject, text, html)
            log.info("email sent: %s", subject)
            for event, groups in findings:
                remember(state, event, groups)
    elif not problem:
        log.info("nothing new")

    health = health_message(state, problem, now)
    if health:
        subject, text = health
        if dry_run:
            print("\n" + subject + "\n\n" + text)
        else:
            try:
                notifier.send_email(subject, text, f"<pre>{htmllib.escape(text)}</pre>")
                log.info("email sent: %s", subject)
            except Exception:
                log.exception("could not send health email")
                state["health"].pop("alert_sent", None)  # try again next run

    if not dry_run:
        save_state(state)
    return 2 if problem else 0


def send_test_alert() -> int:
    """Send a sample failure email to check the mail setup."""
    state = {"health": {"failed_runs": config.ALERT_AFTER_FAILED_RUNS - 1}}
    subject, text = health_message(state, "This is a test alert sent with --test-alert.", datetime.now())
    notifier.send_email("[TEST] " + subject, text, f"<pre>{htmllib.escape(text)}</pre>")
    log.info("test alert sent")
    return 0


def setup_logging(verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(config.LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(file_handler)
    log.addHandler(console)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print findings; no email, no state change")
    parser.add_argument("--test-alert", action="store_true", help="send a sample 'scraper failing' email and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    notifier.load_env(config.ENV_FILE)
    if not args.dry_run and not (os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD")):
        log.error("GMAIL_USER and GMAIL_APP_PASSWORD must be set (see .env.example)")
        return 1
    try:
        if args.test_alert:
            return send_test_alert()
        return run(args.dry_run)
    except Exception:
        log.exception("run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
