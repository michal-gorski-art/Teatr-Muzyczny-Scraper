import copy
import json
from datetime import date
from pathlib import Path

import pytest

import notifier
import scraper

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def seat_page():
    return scraper.parse_seat_page((FIX / "seat_page_duza_scena.html").read_text(encoding="utf-8"))


@pytest.fixture
def free_json():
    return json.loads((FIX / "miejsca_wolne.json").read_text(encoding="utf-8"))


def _seat_id(data, positions, sections, section, row, number):
    for s in scraper.parse_free_json(data):
        if sections.get(s.id) == section and s.row == row and s.number == number:
            return s.id
    raise LookupError((section, row, number))


def _free(data, ids):
    data = copy.deepcopy(data)
    for item in data["miejsca"]:
        item["class"] = "w w3" if item["id"] in ids else "x"  # live site adds a price tier
    return data


def test_parse_seat_page(seat_page):
    params, positions, labels = seat_page
    assert params == {
        "wiz_id": "23",
        "wiz_idt": "bb40faebf12e43fd1211ddd40d626566",
        "ter_id": "3348",
        "ter_idt": "3a57dbda258ff5702654fba558c65799",
        "cen_id": "187",
    }
    assert len(positions) == 1070
    names = {l[0] for l in labels}
    assert {"PARTER", "AMFITEATR", "LOŻA LEWA", "LOŻA PRAWA", "BALKON I", "BALKON II"} <= names


def test_sections_match_hall_plan(seat_page, free_json):
    _, positions, labels = seat_page
    sections = scraper.assign_sections(positions, labels)
    seats = scraper.parse_free_json(free_json)
    rows = {}
    for s in seats:
        rows.setdefault(sections.get(s.id), {}).setdefault(s.row, set()).add(s.number)

    # Counts from the published Duża Scena plan.
    assert {r: len(n) for r, n in rows["PARTER"].items()} == {1: 26, 2: 28, 3: 30, 4: 32, 5: 36}
    assert [len(rows["AMFITEATR"][r]) for r in range(1, 9)] == [20, 21, 20, 20, 20, 19, 19, 18]
    assert [len(rows["LOŻA LEWA"][r]) for r in range(1, 7)] == [9, 9, 10, 11, 12, 13]
    assert [len(rows["LOŻA PRAWA"][r]) for r in range(1, 7)] == [10, 10, 11, 12, 12, 13]
    assert set(rows["BALKON I"]) == set(range(1, 9))
    assert set(rows["BALKON II"]) == set(range(1, 10))


def test_sold_out_event_has_no_groups(seat_page, free_json):
    _, positions, labels = seat_page
    sections = scraper.assign_sections(positions, labels)
    assert scraper.find_groups(scraper.parse_free_json(free_json), positions, sections) == []


def test_pair_in_watched_row_is_found(seat_page, free_json):
    _, positions, labels = seat_page
    sections = scraper.assign_sections(positions, labels)
    ids = {_seat_id(free_json, positions, sections, "PARTER", 3, n) for n in (7, 8, 9)}
    groups = scraper.find_groups(scraper.parse_free_json(_free(free_json, ids)), positions, sections)
    assert len(groups) == 1
    g = groups[0]
    assert (g.section, g.row, [s.number for s in g.seats]) == ("PARTER", 3, [7, 8, 9])
    assert "175 zł" in g.seats[0].price


def test_single_seat_is_ignored(seat_page, free_json):
    _, positions, labels = seat_page
    sections = scraper.assign_sections(positions, labels)
    ids = {_seat_id(free_json, positions, sections, "PARTER", 1, 5),
           _seat_id(free_json, positions, sections, "PARTER", 1, 7)}
    assert scraper.find_groups(scraper.parse_free_json(_free(free_json, ids)), positions, sections) == []


def test_seats_across_the_aisle_are_not_together(seat_page, free_json):
    # Parter row I: seat 13 is left of the aisle, 14 is right of it.
    _, positions, labels = seat_page
    sections = scraper.assign_sections(positions, labels)
    ids = {_seat_id(free_json, positions, sections, "PARTER", 1, n) for n in (13, 14)}
    assert scraper.find_groups(scraper.parse_free_json(_free(free_json, ids)), positions, sections) == []


@pytest.mark.parametrize("section,row,watched", [
    ("PARTER", 5, True),
    ("AMFITEATR", 3, True),
    ("AMFITEATR", 4, False),
    ("LOŻA LEWA", 3, True),
    ("LOŻA PRAWA", 4, False),
    ("BALKON I", 1, False),
])
def test_watched_rows(seat_page, free_json, section, row, watched):
    _, positions, labels = seat_page
    sections = scraper.assign_sections(positions, labels)
    ids = {_seat_id(free_json, positions, sections, section, row, n) for n in (2, 3)}
    groups = scraper.find_groups(scraper.parse_free_json(_free(free_json, ids)), positions, sections)
    assert bool(groups) is watched


def _event(event_id="1", day=date(2030, 1, 1)):
    return scraper.Event(event_id, "abc", "TEST", day, "19.00", "Duża Scena", "https://example.invalid/x")


def _group(*ids):
    return scraper.SeatGroup("PARTER", 1, [scraper.Seat(i, 1, i, "", True) for i in ids])


def test_already_notified_group_is_not_repeated():
    state = {"notified": {}}
    ev = _event()
    scraper.remember(state, ev, [_group(1, 2)])
    assert scraper.new_groups(state, ev, [_group(1, 2)]) == []
    # A neighbour freeing up makes the group news again.
    assert len(scraper.new_groups(state, ev, [_group(1, 2, 3)])) == 1
    # Same seat ids at another performance are separate.
    assert len(scraper.new_groups(state, _event("2"), [_group(1, 2)])) == 1


def test_past_events_are_pruned():
    state = {"notified": {}}
    scraper.remember(state, _event("old", date(2020, 1, 1)), [_group(1, 2)])
    scraper.remember(state, _event("new", date(2030, 1, 1)), [_group(1, 2)])
    scraper.prune_state(state, date(2026, 1, 1))
    assert list(state["notified"]) == ["new"]


def test_parse_repertoire_keeps_numbered_duza_scena_only():
    html = """
    <div class="row spektakl_row kalendarium_row" data-day="24" data-month="11" data-year="2026">
      <div class="spektakl_szczegoly"><div class="h2">MĘSKIE BRZMIENIA</div>
        <div class="h6">17.30 / Duża Scena / <span>KONCERT GOŚCINNY</span></div></div>
      <div class="spektakl_kup"><a href="https://bilety.muzyczny.org/rezerwacja/numerowane.html?id=3348&identyfikator=3a57">Kup bilet</a></div>
    </div>
    <div class="row spektakl_row kalendarium_row" data-day="25" data-month="11" data-year="2026">
      <div class="spektakl_szczegoly"><div class="h2">KAMERALNY</div><div class="h6">19.00 / Scena Kameralna</div></div>
      <div class="spektakl_kup"><a href="https://bilety.muzyczny.org/rezerwacja/numerowane.html?id=9&identyfikator=z">Kup bilet</a></div>
    </div>
    <div class="row spektakl_row kalendarium_row" data-day="26" data-month="11" data-year="2026">
      <div class="spektakl_szczegoly"><div class="h2">BEZ BILETU</div><div class="h6">11.00 / Duża Scena</div></div>
    </div>"""
    events = scraper.parse_repertoire(html)
    assert len(events) == 1
    e = events[0]
    assert (e.id, e.ident, e.title, e.date, e.time, e.stage) == (
        "3348", "3a57", "MĘSKIE BRZMIENIA", date(2026, 11, 24), "17.30", "Duża Scena")


def test_email_lists_seats_and_link():
    subject, text, html = notifier.build_message([(_event(), [_group(4, 5)])])
    assert "(1)" in subject
    assert "PARTER, rząd 1: miejsca 4, 5" in text
    assert "https://example.invalid/x" in html
