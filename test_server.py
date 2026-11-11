import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from server import make_server


class VotingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "votes.sqlite3"
        self.server = make_server("127.0.0.1", 0, self.db, "test-password")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        status, data = self.request("/api/admin/login", {"password": "test-password"})
        self.assertEqual(status, 200)
        self.token = data["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, admin=False, headers=None):
        request_headers = {"Content-Type": "application/json"}
        if admin:
            request_headers["Authorization"] = "Bearer " + self.token
        request_headers.update(headers or {})
        request = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(), headers=request_headers)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def seed(self):
        for name, members in [("甲队", ["甲一"]), ("乙队", ["乙一", "乙二"]), ("丙队", ["丙一", "丙二", "丙三"])]:
            self.assertEqual(self.request("/api/admin/teams", {"name": name, "members": members}, True)[0], 200)
        return self.request("/api/admin/state", admin=True)[1]

    def open(self):
        self.assertEqual(self.request("/api/admin/phase", {"phase": "open"}, True)[0], 200)

    def ballot(self, state, member=None):
        member = member or state["members"][0]
        other = [t["id"] for t in state["teams"] if t["id"] != member["team_id"]]
        return {"code": member["code"], "vote1": other[0], "vote2": other[1]}

    def test_admin_auth_and_private_data(self):
        self.seed()
        self.assertEqual(self.request("/api/admin/state")[0], 401)
        self.assertEqual(self.request("/api/admin/teams", {"name": "假队", "members": ["假人"]})[0], 401)
        self.assertEqual(self.request("/api/admin/login", {"password": "wrong"})[0], 401)
        public = self.request("/api/status")[1]
        self.assertNotIn("members", public)
        self.assertNotIn("ranking", public)
        self.assertNotIn("score", json.dumps(public))
        self.assertNotIn("code", json.dumps(public))
        self.assertEqual(self.request("/votes.sqlite3")[0], 404)
        self.assertEqual(self.request("/api/admin/logout", {}, True)[0], 200)
        self.assertEqual(self.request("/api/admin/state", admin=True)[0], 401)

    def test_identity_and_duplicate_vote_across_clients(self):
        state = self.seed()
        self.open()
        member = state["members"][0]
        status, identity = self.request("/api/identify", {"code": " " + member["code"].lower() + " "})
        self.assertEqual(status, 200)
        self.assertEqual(identity["team_id"], member["team_id"])
        self.assertFalse(identity["voted"])
        self.assertEqual(self.request("/api/vote", self.ballot(state))[0], 200)
        self.assertEqual(self.request("/api/vote", self.ballot(state))[0], 409)
        self.assertTrue(self.request("/api/identify", {"code": member["code"]})[1]["voted"])
        self.assertEqual(self.request("/api/status")[1]["submitted"], 1)

    def test_parallel_duplicate_only_records_once(self):
        state = self.seed()
        self.open()
        ballot = self.ballot(state)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.request("/api/vote", ballot)[0], range(8)))
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 7)
        self.assertEqual(self.request("/api/status")[1]["submitted"], 1)

    def test_ballot_validation_and_no_partial_votes(self):
        state = self.seed()
        self.open()
        valid = self.ballot(state)
        for update in ({"code": ""}, {"code": "BAD"}, {"code": 42}, {"vote1": ""}, {"vote1": None}, {"vote1": True}, {"vote1": 9999}, {"vote1": state["members"][0]["team_id"]}, {"vote2": valid["vote1"]}):
            with self.subTest(update=update):
                self.assertIn(self.request("/api/vote", {**valid, **update})[0], (400, 401))
        self.assertEqual(self.request("/api/status")[1]["submitted"], 0)
        self.assertEqual(self.request("/api/vote", valid)[0], 200)

    def test_equal_total_team_weight(self):
        state = self.seed()
        self.open()
        for member in state["members"]:
            self.assertEqual(self.request("/api/vote", self.ballot(state, member))[0], 200)
        result = self.request("/api/admin/state", admin=True)[1]
        self.assertEqual(result["submitted"], 6)
        for team in result["ranking"]:
            self.assertAlmostEqual(team["score"], 1.0)
        self.assertEqual({team["name"]: team["vote_count"] for team in result["ranking"]}, {"甲队": 5, "乙队": 4, "丙队": 3})

    def test_phase_locking_and_pause(self):
        self.assertEqual(self.request("/api/admin/phase", {"phase": "open"}, True)[0], 400)
        state = self.seed()
        self.assertEqual(self.request("/api/vote", self.ballot(state))[0], 409)
        self.open()
        self.assertEqual(self.request("/api/admin/teams", {"name": "丁队", "members": ["丁"]}, True)[0], 409)
        self.assertEqual(self.request("/api/admin/delete-team", {"id": state["teams"][0]["id"]}, True)[0], 409)
        self.assertEqual(self.request("/api/admin/phase", {"phase": "closed"}, True)[0], 200)
        self.assertEqual(self.request("/api/vote", self.ballot(state))[0], 409)
        self.assertEqual(self.request("/api/admin/phase", {"phase": "draft"}, True)[0], 400)
        self.open()
        self.assertEqual(self.request("/api/vote", self.ballot(state))[0], 200)

    def test_member_validation_and_stable_team_ids(self):
        for members in ([], [""], ["A", "A"], ["A"] * 4, "A", [1]):
            self.assertEqual(self.request("/api/admin/teams", {"name": "队伍", "members": members}, True)[0], 400)
        state = self.seed()
        original = state["teams"][1]["id"]
        deleted_code = state["members"][0]["code"]
        self.assertEqual(self.request("/api/admin/delete-team", {"id": state["teams"][0]["id"]}, True)[0], 200)
        self.assertEqual(self.request("/api/admin/state", admin=True)[1]["teams"][0]["id"], original)
        self.assertEqual(self.request("/api/identify", {"code": deleted_code})[0], 401)

    def test_more_than_three_teams_and_append_while_paused(self):
        state = self.seed()
        for name in ("丁队", "戊队"):
            self.assertEqual(self.request("/api/admin/teams", {"name": name, "members": [name + "成员"]}, True)[0], 200)
        self.assertEqual(len(self.request("/api/status")[1]["teams"]), 5)
        self.open()
        self.assertEqual(self.request("/api/vote", self.ballot(state))[0], 200)
        before = self.request("/api/admin/state", admin=True)[1]["ranking"]
        self.request("/api/admin/phase", {"phase": "closed"}, True)
        self.assertEqual(self.request("/api/admin/teams", {"name": "己队", "members": ["己一"]}, True)[0], 200)
        after = self.request("/api/admin/state", admin=True)[1]
        self.assertEqual(len(after["teams"]), 6)
        self.assertEqual(after["submitted"], 1)
        scores = {t["id"]: t["score"] for t in after["ranking"]}
        for team in before:
            self.assertEqual(scores[team["id"]], team["score"])
        self.open()
        new_member = after["members"][-1]
        self.assertEqual(self.request("/api/vote", self.ballot(after, new_member))[0], 200)

    def test_restart_preserves_codes_and_votes(self):
        state = self.seed()
        self.open()
        self.request("/api/vote", self.ballot(state))
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = make_server("127.0.0.1", 0, self.db, "new-password")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.assertEqual(self.request("/api/status")[1]["submitted"], 1)
        self.assertTrue(self.request("/api/identify", {"code": state["members"][0]["code"]})[1]["voted"])
        self.assertEqual(self.request("/api/admin/state", admin=True)[0], 401)

    def test_cross_origin_rejected(self):
        self.assertEqual(self.request("/api/admin/login", {"password": "test-password"}, headers={"Origin": "https://example.invalid"})[0], 403)

    def test_forwarded_ip_ignored_without_explicit_trust(self):
        for i in range(9):
            self.assertEqual(self.request("/api/admin/login", {"password": "wrong"}, headers={"X-Real-IP": f"198.51.100.{i + 1}"})[0], 401)
        self.assertEqual(self.request("/api/admin/login", {"password": "test-password"}, headers={"X-Real-IP": "203.0.113.1"})[0], 429)

    def test_local_trusted_proxy_keeps_client_limits_separate(self):
        proxy_server = make_server("127.0.0.1", 0, Path(self.temp.name) / "proxy.sqlite3", "test-password", trust_proxy=True)
        thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
        thread.start()
        original_base = self.base
        self.base = f"http://127.0.0.1:{proxy_server.server_port}"
        try:
            for _ in range(10):
                self.assertEqual(self.request("/api/admin/login", {"password": "wrong"}, headers={"X-Real-IP": "198.51.100.1"})[0], 401)
            self.assertEqual(self.request("/api/admin/login", {"password": "test-password"}, headers={"X-Real-IP": "198.51.100.1"})[0], 429)
            self.assertEqual(self.request("/api/admin/login", {"password": "test-password"}, headers={"X-Real-IP": "198.51.100.2", "Host": "203.0.113.5", "Origin": "http://203.0.113.5"})[0], 200)
        finally:
            self.base = original_base
            proxy_server.shutdown()
            proxy_server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
