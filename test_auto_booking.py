import unittest

import watch


def slot(slot_id, start, end, price="1000.00"):
    return {
        "id": slot_id,
        "start_time": start,
        "end_time": end,
        "is_available": True,
        "available_count": 1,
        "price": price,
    }


def inventory(facility_id, facility_name, *slots):
    return [
        {"facility_id": facility_id, "facility_name": facility_name, "slot": item}
        for item in slots
    ]


POLICY = {
    "venue": "Willingdon",
    "days": [0, 1, 2, 3, 4],
    "earliest_start": "19:00",
    "latest_end": "22:00",
    "duration_minutes": 120,
    "max_court_switches": 3,
}


class AutoBookingPlannerTests(unittest.TestCase):
    def test_prefers_a_single_court_for_the_full_session(self):
        data = inventory(
            "court-1", "Padel Court 1",
            slot("a", "2026-07-20 19:00:00", "2026-07-20 20:00:00"),
            slot("b", "2026-07-20 20:00:00", "2026-07-20 21:00:00"),
        ) + inventory(
            "court-2", "Padel Court 2",
            slot("c", "2026-07-20 19:00:00", "2026-07-20 20:00:00"),
        ) + inventory(
            "court-3", "Padel Court 3",
            slot("d", "2026-07-20 20:00:00", "2026-07-20 21:00:00"),
        )

        plans = watch.build_auto_booking_plans(data, POLICY)

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["court_switches"], 0)
        self.assertEqual(len(plans[0]["booking_lines"]), 1)
        self.assertEqual(plans[0]["booking_lines"][0]["facility_id"], "court-1")

    def test_stitches_two_consecutive_courts(self):
        data = inventory(
            "court-1", "Padel Court 1",
            slot("a", "2026-07-20 19:00:00", "2026-07-20 20:00:00"),
        ) + inventory(
            "court-2", "Padel Court 2",
            slot("b", "2026-07-20 20:00:00", "2026-07-20 21:00:00"),
        )

        plan = watch.build_auto_booking_plans(data, POLICY)[0]

        self.assertEqual(plan["start"], "19:00")
        self.assertEqual(plan["end"], "21:00")
        self.assertEqual(plan["court_switches"], 1)
        self.assertEqual([line["facility_id"] for line in plan["booking_lines"]], ["court-1", "court-2"])

    def test_stitches_more_than_two_courts_when_needed(self):
        data = inventory(
            "court-1", "Padel Court 1",
            slot("a", "2026-07-20 19:00:00", "2026-07-20 19:30:00"),
        ) + inventory(
            "court-2", "Padel Court 2",
            slot("b", "2026-07-20 19:30:00", "2026-07-20 20:00:00"),
        ) + inventory(
            "court-3", "Padel Court 3",
            slot("c", "2026-07-20 20:00:00", "2026-07-20 20:30:00"),
        ) + inventory(
            "court-4", "Padel Court 4",
            slot("d", "2026-07-20 20:30:00", "2026-07-20 21:00:00"),
        )

        plan = watch.build_auto_booking_plans(data, POLICY)[0]

        self.assertEqual(plan["court_switches"], 3)
        self.assertEqual(len(plan["booking_lines"]), 4)

    def test_never_books_weekends_or_after_the_evening_boundary(self):
        data = inventory(
            "court-1", "Padel Court 1",
            slot("sat-a", "2026-07-18 19:00:00", "2026-07-18 20:00:00"),
            slot("sat-b", "2026-07-18 20:00:00", "2026-07-18 21:00:00"),
            slot("late-a", "2026-07-20 21:00:00", "2026-07-20 22:00:00"),
            slot("late-b", "2026-07-20 22:00:00", "2026-07-20 23:00:00"),
        )

        self.assertEqual(watch.build_auto_booking_plans(data, POLICY), [])

    def test_requires_exactly_continuous_120_minutes(self):
        data = inventory(
            "court-1", "Padel Court 1",
            slot("a", "2026-07-20 19:00:00", "2026-07-20 20:00:00"),
        ) + inventory(
            "court-2", "Padel Court 2",
            slot("b", "2026-07-20 20:30:00", "2026-07-20 21:30:00"),
        )

        self.assertEqual(watch.build_auto_booking_plans(data, POLICY), [])


if __name__ == "__main__":
    unittest.main()
