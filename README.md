# Muzyczny-Scraper

Watches [Teatr Muzyczny Gdynia](https://muzyczny.org/pl/repertuar.html) for free seats near the
stage on Duża Scena and emails you when **2 or more seats side by side** come up. A seat group is
reported once; you only hear about it again if a seat that hasn't been reported before joins it.

Watched rows (edit `WATCHED_ROWS` in `config.py`):

| Section    | Rows |
|------------|------|
| Parter     | I–V  |
| Amfiteatr  | I–III |
| Loża lewa  | 1–3  |
| Loża prawa | 1–3  |

## How it works

1. Reads every performance with a numbered-seat "Kup bilet" link from the repertoire page.
2. For each one, opens the ticket page to get the seat map (SVG) and its IDs, then asks
   `miejsca-wolne.html` for each seat's status (`w` = free).
3. Works out each seat's section from where it sits on the map (row numbers restart in every
   section), and finds neighbouring free seats by position, so seats on either side of an aisle
   never count as together.
4. Emails new finds in one message and records them in `state.json`. Past performances are
   dropped from the state automatically.

## Failure alerts

If the site changes and the scraper can no longer read it, you would otherwise just stop getting
emails. So it also emails **"Teatr Muzyczny: scraper nie działa"** when a run fails, meaning:

- the repertoire page can't be loaded, or shows no Duża Scena performances with "Kup bilet", or
- at least half the performances can't be checked (seat map or seat data not in the expected
  format, none of the watched sections on the map, and so on).

To avoid noise from a short outage, the alert only goes out after 3 failed runs in a row
(1.5 hours with cron every 30 minutes). It repeats at most once a day while the problem lasts,
and you get one "scraper działa ponownie" email when it works again. The thresholds are the
`ALERT_*` settings in `config.py`. To check that alert emails arrive:

```bash
./run.sh --test-alert
```

## Setup (Raspberry Pi)

```bash
git clone <this repo> ~/Muzyczny-Scraper
cd ~/Muzyczny-Scraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO
chmod 600 .env
./run.sh --dry-run     # prints what it finds; sends nothing, saves nothing
./run.sh --test-alert  # sends a sample failure alert to check the email setup
./run.sh               # first real run: emails everything currently free
```

The Gmail account needs 2-step verification and an
[app password](https://myaccount.google.com/apppasswords).

Cron (`crontab -e`), every 30 minutes from 8:00 to 23:30:

```
*/30 8-23 * * * /home/pi/Muzyczny-Scraper/run.sh >/dev/null 2>&1
```

Logs go to `scraper.log` (rotated at 1 MB). A full run takes a few minutes because it waits
between requests to go easy on the site.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
