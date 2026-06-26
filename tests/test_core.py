"""Unit tests for the pure / easily-isolated logic.

Run with:  .venv\\Scripts\\python.exe -m unittest discover -s tests
No network: every test stubs the EarthMC calls. These guard the invariants the
rest of the codebase leans on (point-in-polygon detection, town ranking, the live
Discord session lifecycle + @Mercenary escalation, new-player caching, and the
players.json robustness guard).
"""
import unittest

from wildness_monitor import geometry, towns, earthmc_api, tracking, alerts


class TestGeometry(unittest.TestCase):
    def test_cardinals(self):
        # +X = East, +Z = South; bearing 0 = North.
        self.assertEqual(geometry.heading(0, -10), "North")
        self.assertEqual(geometry.heading(10, 0), "East")
        self.assertEqual(geometry.heading(0, 10), "South")
        self.assertEqual(geometry.heading(-10, 0), "West")
        self.assertEqual(geometry.heading(10, -10), "North East")


class TestWildernessIndex(unittest.TestCase):
    def setUp(self):
        # One 100x100 square claim named "Tbilisi" centered on origin.
        towns._claims_index = [
            (-50, -50, 50, 50, [[(-50, -50), (50, -50), (50, 50), (-50, 50)]], "Tbilisi")
        ]

    def tearDown(self):
        towns._claims_index = None

    def test_inside_is_not_wilderness(self):
        self.assertIs(towns.is_wilderness(0, 0), False)

    def test_outside_is_wilderness(self):
        self.assertIs(towns.is_wilderness(500, 500), True)

    def test_cold_index_returns_none(self):
        towns._claims_index = None
        self.assertIsNone(towns.is_wilderness(0, 0))

    def test_town_at_names_containing_town(self):
        self.assertEqual(towns.town_at(0, 0), "Tbilisi")

    def test_town_at_wilderness_is_none(self):
        self.assertIsNone(towns.town_at(500, 500))


class TestClosestTowns(unittest.TestCase):
    def setUp(self):
        towns._open_towns_cache = [
            {"name": "Near", "x": 10, "z": 0, "is_capital": False, "nation": "a"},
            {"name": "Far", "x": 900, "z": 0, "is_capital": False, "nation": "b"},
            {"name": "Mid", "x": 100, "z": 0, "is_capital": False, "nation": "c"},
            {"name": "NationSpawn", "x": 300, "z": 0, "is_capital": True, "nation": "Iberia"},
        ]

    def tearDown(self):
        towns._open_towns_cache = []

    def test_reserves_one_nation_slot_and_sorts(self):
        res = towns.find_closest_open_towns(0, 0, n=3)
        self.assertEqual(len(res), 3)
        # Exactly one nation spawn reserved even though 2 towns are closer than it.
        self.assertEqual(sum(1 for t in res if t["is_capital"]), 1)
        # Nearest-first overall.
        self.assertEqual([t["distance"] for t in res], sorted(t["distance"] for t in res))
        self.assertEqual(res[0]["name"], "Near")

    def test_cold_cache_empty(self):
        towns._open_towns_cache = []
        self.assertEqual(towns.find_closest_open_towns(0, 0), [])


class _Resp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class TestOnlinePlayersRobustness(unittest.TestCase):
    def test_skips_malformed_entries(self):
        payload = {"players": [
            {"name": "Good", "x": 1, "y": 64, "z": 2, "world": "w"},
            {"name": "NoPos"},                      # missing x/z → skipped
            {"x": 5, "z": 6},                        # missing name → skipped
        ]}
        class _Sess:
            def get(self, *a, **k):
                pass
        orig = earthmc_api.fetch
        earthmc_api.fetch = lambda *a, **k: _Resp(payload)
        try:
            out = earthmc_api.fetch_online_players(_Sess())
        finally:
            earthmc_api.fetch = orig
        self.assertEqual(set(out), {"good"})
        self.assertEqual(out["good"]["x"], 1)


class TestNewPlayerCache(unittest.TestCase):
    def tearDown(self):
        tracking._new_player_cache.clear()

    def test_established_player_cached_and_not_requeried(self):
        calls = []
        def fake_batch(session, names):
            calls.append(list(names))
            return {n.lower(): False for n in names}   # all established
        orig = tracking.check_new_players_batch
        tracking.check_new_players_batch = fake_batch
        try:
            tracking._resolve_new_players(None, ["Bob"])
            tracking._resolve_new_players(None, ["Bob"])   # second time: cached
        finally:
            tracking.check_new_players_batch = orig
        self.assertEqual(calls, [["Bob"]])   # queried once only


class TestAlertLifecycle(unittest.TestCase):
    def setUp(self):
        alerts._sessions.clear()
        alerts._message_ids.clear()
        self._url, self._role = alerts.DISCORD_WEBHOOK_URL, alerts.MERCENARY_ROLE_ID
        alerts.DISCORD_WEBHOOK_URL = "http://x"   # enable
        alerts.MERCENARY_ROLE_ID = "42"
        self.events = []
        self._orig_enqueue = alerts._enqueue
        alerts._enqueue = lambda action, key, payload, label: self.events.append((action, payload["content"]))

    def tearDown(self):
        alerts._enqueue = self._orig_enqueue
        alerts.DISCORD_WEBHOOK_URL, alerts.MERCENARY_ROLE_ID = self._url, self._role
        alerts._sessions.clear()

    def test_entry_update_final(self):
        alerts.send_discord_alert("Bob", "(1,2)", 1, 2, False, towns=None, dwell=0)
        alerts.send_discord_alert("Bob", "(3,4)", 3, 4, False, towns=None, dwell=10)
        alerts.finalize_absent_sessions(set())
        self.assertEqual([a for a, _ in self.events], ["new", "edit", "final"])
        self.assertIn("left wilderness", self.events[-1][1])

    def test_new_player_creates_no_session(self):
        alerts.send_discord_alert("Newbie", "(1,2)", 1, 2, True)
        self.assertNotIn("newbie", alerts._sessions)
        self.assertEqual(self.events, [])

    def test_mercenary_escalation_fires_once_at_threshold(self):
        alerts.PING_ROLE_THRESHOLD = 10
        for i in range(12):
            alerts.send_discord_alert("Bob", "(1,2)", 1, 2, False, towns=None, dwell=i * 10)
        role_pings = [c for a, c in self.events if a == "status" and "<@&42>" in c]
        self.assertEqual(len(role_pings), 1)


if __name__ == "__main__":
    unittest.main()
