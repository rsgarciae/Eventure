"""Unit tests for the parts that can be wrong silently: date guessing, dedupe,
calendar output, and digest labels."""

from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from eventure import collect
from eventure import config as config_module
from eventure import dates, digest, ics, main, sources, store
from eventure.config import Config, Source
from eventure.model import Collected, Event, Signal, SourceHealth

TZ = ZoneInfo("UTC")


def ref(year: int = 2026, month: int = 11, day: int = 21) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=TZ)


class DateExtractionTests(unittest.TestCase):
    def test_spanish_textual_with_time(self):
        got = dates.find("Nos vemos el 28 de noviembre a las 19:00", TZ, ref(), "DMY", ("es", "en"))
        self.assertIsNotNone(got)
        self.assertEqual((got.start.month, got.start.day, got.start.hour), (11, 28, 19))
        self.assertTrue(got.has_time)

    def test_textual_without_time_is_all_day(self):
        got = dates.find("Sábado 5 de diciembre, entrada libre", TZ, ref(), "DMY", ("es",))
        self.assertIsNotNone(got)
        self.assertFalse(got.has_time)
        self.assertEqual((got.start.month, got.start.day), (12, 5))

    def test_iso(self):
        got = dates.find("2027-01-15 doors 21:00", TZ, ref(), "DMY")
        self.assertEqual((got.start.year, got.start.month, got.start.day), (2027, 1, 15))

    def test_pm_marker_is_applied(self):
        got = dates.find(
            "Este sábado 28 de noviembre, arrancamos en punto de las 9:00 pm.",
            TZ, ref(), "DMY", ("es",),
        )
        self.assertEqual(got.start.hour, 21)
        self.assertTrue(got.has_time)

    def test_am_marker_is_applied(self):
        got = dates.find("28 de noviembre a las 9:00 a. m.", TZ, ref(), "DMY", ("es",))
        self.assertEqual(got.start.hour, 9)
        self.assertTrue(got.has_time)

    def test_noon_and_midnight_markers(self):
        noon = dates.find("28 de noviembre 12:00 pm", TZ, ref(), "DMY", ("es",))
        midnight = dates.find("28 de noviembre 12:00 am", TZ, ref(), "DMY", ("es",))
        self.assertEqual(noon.start.hour, 12)
        self.assertEqual(midnight.start.hour, 0)

    def test_spanish_day_part_is_a_time(self):
        got = dates.find("28 de noviembre a las 8 de la noche", TZ, ref(), "DMY", ("es",))
        self.assertEqual(got.start.hour, 20)
        self.assertTrue(got.has_time)

    def test_day_number_without_marker_is_not_a_time(self):
        got = dates.find("Nos vemos el 28 de noviembre, entrada libre", TZ, ref(), "DMY", ("es",))
        self.assertFalse(got.has_time)
        self.assertEqual(got.start.hour, 0)

    def test_weekday_plus_day_number(self):
        # 26 Nov 2026 is a Thursday; ref() is Sat 21 Nov 2026.
        got = dates.find(
            "Este jueves 26 se canta con todo en Bar Rock House", TZ, ref(), "DMY", ("es",)
        )
        self.assertIsNotNone(got)
        self.assertEqual((got.start.month, got.start.day), (11, 26))
        self.assertFalse(got.has_time)

    def test_weekday_that_does_not_match_is_rejected(self):
        # 26 is never a Monday in the next three months, so this must not guess.
        self.assertIsNone(dates.find("lunes 26", TZ, ref(), "DMY", ("es",)))

    def test_day_month_wins_over_a_wrong_weekday(self):
        # 28 Nov 2026 is a Saturday, so the caption's "lunes" is wrong: the
        # explicit day+month must win and must not spawn a second date.
        got = dates.find_all("lunes 28 de noviembre", TZ, ref(), "DMY", ("es",))
        self.assertEqual([(item.start.month, item.start.day) for item in got], [(11, 28)])

    def test_date_order_changes_interpretation(self):
        dmy = dates.find("03/04", TZ, ref(), "DMY")
        mdy = dates.find("03/04", TZ, ref(), "MDY")
        self.assertEqual((dmy.start.month, dmy.start.day), (4, 3))
        self.assertEqual((mdy.start.month, mdy.start.day), (3, 4))

    def test_unambiguous_day_ignores_order(self):
        got = dates.find("25/11", TZ, ref(), "MDY")  # 25 cannot be a month
        self.assertEqual((got.start.month, got.start.day), (11, 25))

    def test_past_month_rolls_to_next_year(self):
        got = dates.find("3 de enero", TZ, ref(), "DMY", ("es",))
        self.assertEqual(got.start.year, 2027)

    def test_impossible_date_is_not_invented(self):
        self.assertIsNone(dates.find("31 de febrero", TZ, ref(), "DMY", ("es",)))
        self.assertIsNone(dates.find("hola qué tal", TZ, ref(), "DMY", ("es",)))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = store.Store.load(Path(self.tmp.name) / "state.json")
        self.now = ref()

    def test_same_uid_is_not_new_twice(self):
        event = Event(uid="a", title="Jazz", start=datetime(2026, 12, 1, 20, tzinfo=TZ))
        self.assertEqual(len(self.state.ingest([event], self.now)), 1)
        self.assertEqual(self.state.ingest([event], self.now), [])

    def test_same_event_from_two_sources_collapses(self):
        first = Event(uid="a", title="Jazz Night", start=datetime(2026, 12, 1, 20, tzinfo=TZ))
        second = Event(uid="b", title="  jazz   night ", start=datetime(2026, 12, 1, 20, tzinfo=TZ))
        self.state.ingest([first], self.now)
        self.assertEqual(self.state.ingest([second], self.now), [])

    def test_different_day_is_a_different_event(self):
        first = Event(uid="a", title="Jazz", start=datetime(2026, 12, 1, 20, tzinfo=TZ))
        second = Event(uid="b", title="Jazz", start=datetime(2026, 12, 2, 20, tzinfo=TZ))
        self.state.ingest([first], self.now)
        self.assertEqual(len(self.state.ingest([second], self.now)), 1)

    def test_same_uid_refreshes_record_but_keeps_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = store.Store(Path(tmp) / "state.json")
            now = datetime(2026, 11, 21, 12, tzinfo=TZ)
            start = datetime(2026, 11, 28, 17, tzinfo=TZ)
            first = Event(uid="u1", title="Tocada", start=start, source="IG")
            state.ingest([first], now)
            state.mark_notified([first], now)
            state.mark_added("u1", "gcal-1", now)
            short_before = state.data["events"]["u1"]["short"]

            # The source later publishes an image and fixes the title.
            again = Event(uid="u1", title="Tocada (corregido)", start=start, source="IG",
                          image="https://cdn.example/late.jpg")
            fresh = state.ingest([again], now)
            record = state.data["events"]["u1"]

        self.assertEqual(fresh, [])  # not a new event
        self.assertEqual(record["title"], "Tocada (corregido)")
        self.assertEqual(record["image"], "https://cdn.example/late.jpg")
        self.assertEqual(record["short"], short_before)  # buttons keep working
        self.assertIsNotNone(record["notified_at"])
        self.assertEqual(record["gcal_id"], "gcal-1")

    def test_fuzzy_duplicate_refreshes_the_canonical_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = store.Store(Path(tmp) / "state.json")
            now = datetime(2026, 11, 21, 12, tzinfo=TZ)
            start = datetime(2026, 11, 28, 17, tzinfo=TZ)
            state.ingest([Event(uid="old", title="Tocada", start=start, source="IG")], now)
            short_before = state.data["events"]["old"]["short"]

            # Same event under a new uid (a post that gained a date suffix), and
            # now with a profile and an image.
            fresh = state.ingest(
                [
                    Event(
                        uid="old#2026-11-28",
                        title="Tocada",
                        start=start,
                        source="IG",
                        channel="centrorevueltas",
                        image="https://cdn.example/x.jpg",
                    )
                ],
                now,
            )
            record = state.data["events"]["old"]

        self.assertEqual(fresh, [])  # not a new event
        self.assertEqual(len(state.data["events"]), 1)  # no duplicate record
        self.assertEqual(record["channel"], "centrorevueltas")
        self.assertEqual(record["image"], "https://cdn.example/x.jpg")
        self.assertEqual(record["short"], short_before)  # old button still works

    def test_short_id_resolves_back(self):
        event = Event(uid="a", title="Jazz", start=datetime(2026, 12, 1, 20, tzinfo=TZ))
        self.state.ingest([event], self.now)
        short = self.state.data["events"]["a"]["short"]
        self.assertEqual(self.state.by_short(short)[0], "a")

    def test_health_marks_failure_without_losing_baseline(self):
        self.state.record_health([SourceHealth(name="x", ok=True, items=10)])
        self.state.record_health([SourceHealth(name="x", ok=False, error="HTTP 500")])
        record = self.state.data["sources"]["x"]
        self.assertGreater(record["consecutive_failures"], 0)
        self.assertEqual(record["last_error"], "HTTP 500")


class CalendarFileTests(unittest.TestCase):
    def test_all_day_uses_date_value_and_escapes(self):
        event = Event(
            uid="x",
            title="Arte, café; y más",
            start=datetime(2026, 12, 1, tzinfo=TZ),
            all_day=True,
        )
        cal = ics.build([event], "UTC")
        self.assertIn("DTSTART;VALUE=DATE:20261201", cal)
        self.assertIn("SUMMARY:Arte\\, café\\; y más", cal)
        self.assertTrue(cal.startswith("BEGIN:VCALENDAR\r\n"))

    def test_lines_fold_at_75_octets(self):
        long_title = "Festival " + "muy largo " * 20
        cal = ics.build([Event(uid="y", title=long_title, start=datetime(2026, 12, 1, tzinfo=TZ))], "UTC")
        for line in cal.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line[:40])

    def test_accented_title_folds_on_bytes_not_characters(self):
        title = "Canción de cuna — edición especial " * 3
        cal = ics.build([Event(uid="z", title=title, start=datetime(2026, 12, 1, tzinfo=TZ))], "UTC")
        for line in cal.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(language="es", timezone="UTC")

    def test_candidate_is_marked_and_button_carries_short_id(self):
        event = Event(
            uid="u1",
            title="Posible tocada",
            start=datetime(2026, 11, 28, 17, tzinfo=TZ),
            confidence="candidate",
        )
        text, keyboard = digest.render([(event, "abcd1234")], [], [SourceHealth("x", True, 1)], self.cfg)
        self.assertIn("⚠", text)
        self.assertEqual(keyboard[0][0]["callback_data"], "a:abcd1234")

    def test_failing_source_is_named_not_hidden(self):
        text, _ = digest.render([], [], [SourceHealth("roto", False, error="HTTP 500")], self.cfg)
        self.assertIn("roto", text)
        self.assertIn("0/1", text)

    def test_quiet_day_says_so_explicitly(self):
        text, _ = digest.render([], [], [SourceHealth("x", True, 0)], self.cfg)
        self.assertIn("Sin novedades", text)

    def test_broken_collection_is_loud(self):
        text = digest.render_broken([SourceHealth("x", False, error="timeout")], self.cfg)
        self.assertIn("NO significa que no haya eventos", text)

    def test_photo_card_caption_and_button(self):
        event = Event(
            uid="u2",
            title="El Cascanueces",
            start=datetime(2026, 11, 28, 17, tzinfo=TZ),
            location="Teatro Ricardo Castro",
            image="https://example.org/poster.jpg",
        )
        caption, keyboard = digest.render_card(event, "deadbeef", self.cfg)
        self.assertIn("El Cascanueces", caption)
        self.assertIn("17:00", caption)
        self.assertEqual(keyboard[0][0]["callback_data"], "a:deadbeef")
        self.assertLessEqual(len(caption), 1024)

    def test_footer_alone_is_valid_when_every_event_is_a_card(self):
        text = digest.render_footer_only([SourceHealth("x", True, 3)], self.cfg)
        self.assertIn("1/1", text)

    def test_event_and_card_name_the_profile(self):
        event = Event(
            uid="u3",
            title="Tocada",
            start=datetime(2026, 11, 28, 21, tzinfo=TZ),
            channel="modelobar_rockhouse",
        )
        text, _ = digest.render([(event, "abcd1234")], [], [SourceHealth("x", True, 1)], self.cfg)
        caption, _ = digest.render_card(event, "abcd1234", self.cfg)
        self.assertIn("@modelobar_rockhouse", text)
        self.assertIn("@modelobar_rockhouse", caption)

    def test_signal_names_the_profile_not_the_feed_name(self):
        signal = Signal(
            uid="s1",
            title="promo sin fecha",
            url="https://www.instagram.com/p/x/",
            source="Instagram feed",
            channel="gozadera_calvario",
        )
        text, _ = digest.render([], [signal], [SourceHealth("x", True, 1)], self.cfg)
        self.assertIn("@gozadera_calvario", text)
        self.assertNotIn("(Instagram feed)", text)


class SchedulingTests(unittest.TestCase):
    def test_next_slot_same_day(self):
        now = datetime(2026, 11, 21, 7, 0, tzinfo=TZ)
        self.assertAlmostEqual(main.seconds_until_next(("08:00", "19:00"), TZ, now), 3600, places=0)

    def test_next_slot_wraps_to_tomorrow(self):
        now = datetime(2026, 11, 21, 20, 0, tzinfo=TZ)
        self.assertAlmostEqual(
            main.seconds_until_next(("08:00", "19:00"), TZ, now), 12 * 3600, places=0
        )

    def test_garbage_times_do_not_crash_the_scheduler(self):
        now = datetime(2026, 11, 21, 7, 0, tzinfo=TZ)
        self.assertGreater(main.seconds_until_next(("nope",), TZ, now), 0)


CONFIG_TOML = """
[general]
language = "es"
timezone = "UTC"
state_path = "data/state.json"

[schedule]
times = ["07:30", "18:30"]
run_on_start = true

[[source]]
name = "x"
type = "ics"
url = "https://example.org/x.ics"
"""


class ConfigTests(unittest.TestCase):
    def test_schedule_and_state_path_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(CONFIG_TOML, encoding="utf-8")
            cfg = config_module.load(path)
        self.assertEqual(cfg.digest_times, ("07:30", "18:30"))
        self.assertTrue(cfg.run_on_start)
        self.assertEqual(cfg.state_path, Path("data/state.json"))

    def test_config_without_sources_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[general]\ntimezone = "UTC"\n', encoding="utf-8")
            with self.assertRaises(config_module.ConfigError):
                config_module.load(path)


class MultiDateTests(unittest.TestCase):
    """One caption can announce several nights, and each is its own event."""

    def test_two_nights_in_one_caption(self):
        got = dates.find_all(
            "Nos vemos viernes 27 y sábado 28 de noviembre", TZ, ref(), "DMY", ("es",)
        )
        self.assertEqual([(item.start.month, item.start.day) for item in got], [(11, 27), (11, 28)])
        self.assertTrue(all(not item.has_time for item in got))

    def test_single_time_applies_to_both_nights(self):
        got = dates.find_all(
            "viernes 27 y sábado 28 de noviembre a las 9 pm", TZ, ref(), "DMY", ("es",)
        )
        self.assertEqual([item.start.hour for item in got], [21, 21])

    def test_two_times_over_two_dates_stay_all_day(self):
        got = dates.find_all(
            "viernes 27 a las 8 pm y sábado 28 de noviembre a las 10 pm",
            TZ, ref(), "DMY", ("es",),
        )
        self.assertEqual(len(got), 2)
        self.assertTrue(all(not item.has_time for item in got))

    def test_single_date_with_a_range_keeps_the_start(self):
        got = dates.find_all("sábado 28 de noviembre. 9 pm — 2 am", TZ, ref(), "DMY", ("es",))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].start.hour, 21)

    def test_find_returns_only_the_first(self):
        got = dates.find(
            "Nos vemos viernes 27 y sábado 28 de noviembre", TZ, ref(), "DMY", ("es",)
        )
        self.assertEqual(got.start.day, 27)


class InstagramFeedTests(unittest.TestCase):
    """The JSON handoff between ig_collect.py and the worker."""

    def test_post_image_reaches_the_event(self):
        soon = datetime.now(timezone.utc) + timedelta(days=5)
        months = [
            "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]
        caption = f"Este día {soon.day} de {months[soon.month - 1]} a las 9:00 pm"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instagram.json"
            path.write_text(
                json.dumps(
                    {
                        "posts": [
                            {
                                "uid": "ig:withimage",
                                "caption": caption,
                                "url": "https://www.instagram.com/p/withimage/",
                                "profile": "venue",
                                "image": "https://cdn.example/photo.jpg",
                            },
                            {
                                "uid": "ig:noimage",
                                "caption": "sin fecha en el texto",
                                "url": "https://www.instagram.com/p/noimage/",
                                "profile": "venue",
                                "image": "",
                            },
                        ],
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )
            cfg = Config(
                language="es",
                timezone="UTC",
                sources=(Source(name="IG", type="instagram_json", path=str(path)),),
            )
            got = collect.collect(cfg)

        self.assertEqual(len(got.events), 1)
        event = got.events[0]
        self.assertEqual(event.uid, "ig:withimage")  # single date keeps its uid
        self.assertEqual(event.image, "https://cdn.example/photo.jpg")
        self.assertEqual(event.start.hour, 21)
        self.assertEqual(len(got.signals), 1)

    def test_one_post_two_nights_becomes_two_events(self):
        months = [
            "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]
        first = (datetime.now(timezone.utc) + timedelta(days=10)).date()
        second = first + timedelta(days=1)
        caption = (
            f"Nos vemos el {first.day} de {months[first.month - 1]} "
            f"y el {second.day} de {months[second.month - 1]}"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instagram.json"
            path.write_text(
                json.dumps({"posts": [{"uid": "ig:x", "caption": caption,
                                       "url": "https://www.instagram.com/p/x/"}],
                            "errors": []}),
                encoding="utf-8",
            )
            cfg = Config(
                language="es",
                timezone="UTC",
                sources=(Source(name="IG", type="instagram_json", path=str(path)),),
            )
            got = collect.collect(cfg)

        self.assertEqual(len(got.events), 2)
        self.assertEqual([event.start.day for event in got.events], [first.day, second.day])
        uids = [event.uid for event in got.events]
        self.assertEqual(len(set(uids)), 2)  # distinct buttons, distinct calendar entries
        # Every date is suffixed: handing the original uid to the first date
        # would rewrite an existing record when a post gains a second night.
        self.assertTrue(all(uid.startswith("ig:x#") for uid in uids))

    def test_missing_image_key_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instagram.json"
            path.write_text(
                json.dumps({"posts": [{"caption": "sin fecha", "url": "https://x/p/"}], "errors": []}),
                encoding="utf-8",
            )
            cfg = Config(
                language="es",
                timezone="UTC",
                sources=(Source(name="IG", type="instagram_json", path=str(path)),),
            )
            got = collect.collect(cfg)
        self.assertEqual(len(got.events), 0)
        self.assertEqual(len(got.signals), 1)
        self.assertEqual(got.signals[0].url, "https://x/p/")
        self.assertTrue(got.health[0].ok)


class DigestSectionTests(unittest.TestCase):
    def _cfg(self, tmp: str) -> Config:
        return Config(
            language="es",
            timezone="UTC",
            state_path=Path(tmp) / "state.json",
            sources=(
                Source(name="Web", type="ics", url="https://example.org/x.ics"),
                Source(name="IG", type="instagram_json", path="instagram.json"),
            ),
        )

    def test_web_and_instagram_go_into_separate_messages(self):
        soon = datetime.now(timezone.utc) + timedelta(days=3)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            state = store.Store(cfg.state_path)
            gathered = Collected(
                events=[
                    Event(uid="w1", title="Exposición web", start=soon, source="Web"),
                    Event(uid="i1", title="Tocada instagram", start=soon, source="IG"),
                ],
                health=[SourceHealth("Web", True, 1), SourceHealth("IG", True, 1)],
            )
            sections, fresh = main._apply_digest(cfg, state, gathered)

        self.assertEqual(fresh, 2)
        self.assertEqual(len(sections), 2)
        texts = [section[0] for section in sections]
        for text in texts:
            self.assertFalse("Exposición web" in text and "Tocada instagram" in text)
        self.assertTrue(any("Exposición web" in text for text in texts))
        self.assertTrue(any("Tocada instagram" in text for text in texts))
        self.assertTrue(any("Páginas web" in text for text in texts))
        self.assertTrue(any("Instagram" in text for text in texts))

    def test_empty_group_costs_no_message(self):
        soon = datetime.now(timezone.utc) + timedelta(days=3)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            state = store.Store(cfg.state_path)
            gathered = Collected(
                events=[Event(uid="w1", title="Solo web", start=soon, source="Web")],
                health=[SourceHealth("Web", True, 1), SourceHealth("IG", True, 0)],
            )
            sections, _fresh = main._apply_digest(cfg, state, gathered)
        self.assertEqual(len(sections), 1)
        self.assertIn("Solo web", sections[0][0])

    def test_resend_repeats_pending_but_not_added_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = store.Store(Path(tmp) / "state.json")
            state.data["events"] = {
                "pending": {"notified_at": "2026-01-01T00:00:00", "added_at": None},
                "added": {"notified_at": "2026-01-01T00:00:00", "added_at": "2026-01-02T00:00:00"},
            }
            state.data["signals"] = {"s1": "2026-01-01T00:00:00"}
            events, signals = state.reset_notified()

        self.assertEqual((events, signals), (1, 1))
        self.assertIsNone(state.data["events"]["pending"]["notified_at"])
        self.assertEqual(state.data["events"]["added"]["notified_at"], "2026-01-01T00:00:00")
        self.assertEqual(state.data["signals"], {})

    def test_resend_can_include_added_events_for_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = store.Store(Path(tmp) / "state.json")
            state.data["events"] = {
                "added": {"notified_at": "2026-01-01T00:00:00", "added_at": "2026-01-02T00:00:00"},
            }
            events, _signals = state.reset_notified(include_added=True)
            record = state.data["events"]["added"]

        self.assertEqual(events, 1)
        self.assertIsNone(record["notified_at"])
        # The calendar entry is kept: pressing the button replies "already added".
        self.assertEqual(record["added_at"], "2026-01-02T00:00:00")


class SourceImportTests(unittest.TestCase):
    """sources.txt -> config.toml, without ever writing broken TOML."""

    BASE = '''[general]
timezone = "UTC"

[[source]]
name = "Existing"
type = "ics"
url = "https://example.org/events/?ical=1"
'''

    def test_key_ignores_instagram_query_but_keeps_ical(self):
        self.assertEqual(
            sources.key("https://www.instagram.com/venue/?knvt=abc"), "www.instagram.com/venue"
        )
        self.assertEqual(
            sources.key("https://durango.com.mx/eventos/?ical=1"),
            "durango.com.mx/eventos?ical=1",
        )
        # The HTML page and its iCal export are not the same source.
        self.assertNotEqual(
            sources.key("https://example.org/eventos/"),
            sources.key("https://example.org/eventos/?ical=1"),
        )

    def test_merge_skips_duplicates_and_appends_new(self):
        text, added, skipped = sources.merge(
            self.BASE,
            [
                ("Existing", "ics", "https://example.org/events/?ical=1", ""),
                ("New venue", "ics", "https://new.example/events.ics", ""),
            ],
        )
        self.assertEqual(len(added), 1)
        self.assertEqual(skipped[0][0], "Existing")
        self.assertEqual(len(tomllib.loads(text)["source"]), 2)
        self.assertEqual(
            tomllib.loads(text)["source"][1]["url"], "https://new.example/events.ics"
        )

    def test_instagram_profile_pulls_in_the_feed_reader(self):
        text, _added, _skipped = sources.merge(
            self.BASE,
            [("venue", "instagram_profile", "https://www.instagram.com/venue/", "")],
        )
        kinds = [source["type"] for source in tomllib.loads(text)["source"]]
        self.assertIn("instagram_profile", kinds)
        # Without this the worker has profiles but nothing to read the JSON from.
        self.assertIn("instagram_json", kinds)

    def test_name_with_a_quote_still_yields_valid_toml(self):
        text, _added, _skipped = sources.merge(
            self.BASE, [('Venue "El Rock"', "ics", "https://x.example/f.ics", "")]
        )
        self.assertEqual(
            tomllib.loads(text)["source"][-1]["name"], 'Venue "El Rock"'
        )

    def test_known_config_is_left_byte_for_byte(self):
        text, added, _skipped = sources.merge(
            self.BASE, [("Existing", "ics", "https://example.org/events/?ical=1", "")]
        )
        self.assertEqual(added, [])
        self.assertEqual(text, self.BASE)

    def test_page_key_treats_a_page_and_its_ical_as_one_source(self):
        self.assertEqual(
            sources.page_key("https://durango.com.mx/eventos/"),
            sources.page_key("https://durango.com.mx/eventos/?ical=1"),
        )
        # A different path is a different page: /feed/ is not /eventos/.
        self.assertNotEqual(
            sources.page_key("https://durango.com.mx/feed/"),
            sources.page_key("https://durango.com.mx/eventos/?ical=1"),
        )


if __name__ == "__main__":
    unittest.main()
