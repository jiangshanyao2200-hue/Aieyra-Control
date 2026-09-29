"""Real HTTP native proof, durable forum data, and local growth boundaries."""

import json
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from test_cloud import cloud
from test_control import control
from cloud_link import CloudLink, CloudError
from feedback_contract import FeedbackError
import matrix_proof
import test_agent_access as agent_fixture
import cloud_link


def topic(rid="matrix-test-topic-0001"):
    return {
        "requestId": rid,
        "type": "bug",
        "title": "Update reconnect issue",
        "summary": "A synthetic product diagnostic.",
        "content": "Reproduction: disconnect a fixture then reconnect. Expected recovery; observed a retry error.",
        "publication": "public",
        "confirmed": True,
        "growthId": "fixture-reconnect",
    }


class MatrixHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = cloud.Cloud(
            self.temp.name, identity=lambda cookie: {"subject": cookie, "name": "Fixture"}
        )
        self.server = cloud.BoundedServer(("127.0.0.1", 0), cloud.Handler)
        self.server.app = self.app
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.opener = build_opener(ProxyHandler({}))
        self.session = self.login()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, path, body=None, session=None, headers=None, proof=True):
        head = {"Accept": "application/json"}
        raw = None if body is None else matrix_proof.encode(body)
        if body is not None:
            head["Content-Type"] = "application/json"
        if session:
            head["Authorization"] = "Bearer " + session["access_token"]
        if body is not None and session and proof and path.startswith("/v1/matrix/"):
            head.update(matrix_proof.headers(path, body, session))
        head.update(headers or {})
        try:
            with self.opener.open(
                Request(self.base + path, data=raw, headers=head), timeout=5
            ) as response:
                return response.status, json.load(response)
        except HTTPError as e:
            with e:
                return e.code, json.load(e)

    def login(self, subject="fixture", scope="desktop", bound=True):
        keys = matrix_proof.run({"action": "generate"})
        verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        body = {
            "challenge": cloud.challenge(verifier),
            "state": state,
            "scope": scope,
            "redirect_uri": cloud.CLOUD + "/auth/callback",
        }
        if bound:
            body["public_key"] = keys["publicKey"]
        status, start = self.request("/v1/auth/start", body)
        self.assertEqual(status, 200, start)
        redirect = self.app.authorize(start["flow_id"], subject)
        code = parse_qs(urlsplit(redirect).query)["code"][0]
        session = self.app.exchange(
            {
                "flow_id": start["flow_id"],
                "code": code,
                "state": state,
                "verifier": verifier,
                "redirect_uri": body["redirect_uri"],
            }
        )
        session["matrix_private_key"] = keys["privateKey"]
        return session

    def test_capabilities_match_enforced_operation_limits(self):
        status, capabilities = self.request("/v1/matrix/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(capabilities["browserReadOnly"])
        self.assertEqual(
            capabilities["limits"]["replyContent"], {"minLength": 1, "maxLength": 4000}
        )
        self.assertEqual(
            capabilities["limits"]["topicContent"], {"minLength": 10, "maxLength": 12000}
        )
        self.assertEqual(set(capabilities["operations"]), {"create", "reply", "state", "withdraw"})
        self.assertTrue(capabilities["retry"]["sameRequestIdAndPayload"])

    def test_public_boards_filter_before_pagination_and_hide_account_names(self):
        created = []
        with patch.dict("os.environ", {"CONTROL_MATRIX_ADMINS": "fixture"}):
            for i, kind in enumerate(["bug", "repair", "discussion", "project", "update"]):
                payload = {**topic("board-" + str(i)), "type": kind}
                if kind == "project":
                    payload["projectUrl"] = "https://example.com/project"
                code, value = self.request("/v1/matrix/topics", payload, self.session)
                self.assertEqual(code, 200, value)
                created.append(value["item"])
        for board, expected in [
            ("releases", {"update"}),
            ("feedback", {"bug", "repair"}),
            ("lounge", {"discussion", "project"}),
        ]:
            code, value = self.request("/v1/matrix/topics?board=" + board)
            self.assertEqual(code, 200)
            self.assertEqual({item["type"] for item in value["items"]}, expected)
            self.assertTrue(all(item["board"] == board for item in value["items"]))
        self.assertEqual(
            self.request("/v1/matrix/topics?board=feedback&type=project")[1]["items"], []
        )
        self.assertEqual(self.request("/v1/matrix/topics?board=unknown")[0], 400)
        alias = created[0]["author"]
        self.assertTrue(alias["name"].startswith("Agent · "))
        self.assertNotIn("Fixture", json.dumps(created))
        self.assertEqual(alias, created[-2]["author"])
        self.assertEqual(created[-1]["author"]["name"], "Aieyra Control")
        reply = {
            "requestId": "alias-reply",
            "content": "Reviewed public reply",
            "publication": "public",
            "confirmed": True,
        }
        self.assertEqual(
            self.request("/v1/matrix/topics/" + created[0]["id"] + "/replies", reply, self.session)[
                0
            ],
            200,
        )
        replies = self.request("/v1/matrix/topics/" + created[0]["id"] + "/replies")[1]["items"]
        self.assertEqual(replies[0]["author"], alias)
        with self.app.db() as db:
            db.execute("UPDATE matrix_topics SET author='Private account name'")
            db.execute("UPDATE matrix_replies SET author='Private account name'")
        self.app.init_matrix()
        self.assertNotIn("Private account name", json.dumps(self.request("/v1/matrix/topics")[1]))
        self.assertNotIn(
            "Private account name",
            json.dumps(self.request("/v1/matrix/topics/" + created[0]["id"] + "/replies")[1]),
        )

    def test_board_pagination_has_no_mixed_or_skipped_types(self):
        for i in range(23):
            payload = {**topic("page-board-" + str(i)), "type": "bug" if i < 21 else "discussion"}
            self.assertEqual(self.request("/v1/matrix/topics", payload, self.session)[0], 200)
        first = self.request("/v1/matrix/topics?board=feedback")[1]
        self.assertEqual(len(first["items"]), 20)
        second = self.request(
            "/v1/matrix/topics?board=feedback&before=" + str(first["nextCursor"])
        )[1]
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["nextCursor"])
        self.assertEqual(len({x["id"] for x in first["items"] + second["items"]}), 21)

    def test_webp_static_mime_and_share_redirect(self):
        for asset in (
            "collaboration.webp",
            "collaboration-small.webp",
            "scene-01-clean.webp",
            "scene-01-clean-small.webp",
            "scene-02-clean.webp",
            "scene-02-clean-small.webp",
            "scene-03-clean.webp",
            "scene-03-clean-small.webp",
            "scene-05-clean.webp",
            "scene-05-clean-small.webp",
            *(
                f"scene-{number:02d}{suffix}.webp"
                for number in range(1, 9)
                for suffix in ("", "-small")
            ),
        ):
            with self.opener.open(self.base + "/assets/" + asset) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], "image/webp")
                self.assertEqual(response.read(4), b"RIFF")
        for script in ("backgrounds.js", "home-demo.js", "transitions.js"):
            with self.opener.open(self.base + "/" + script) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("javascript", response.headers["Content-Type"])
        with self.opener.open(self.base + "/share") as response:
            self.assertTrue(response.url.endswith("/center?type=project"))
            self.assertIn("论坛板块", response.read().decode("utf-8"))

    def test_proof_body_path_session_and_replay_are_bound(self):
        p = topic()
        path = "/v1/matrix/topics"
        headers = matrix_proof.headers(path, p, self.session)
        self.assertEqual(
            self.request(path, {**p, "title": "Changed text"}, self.session, headers)[0], 401
        )
        self.assertEqual(self.request(path + "/wrong", p, self.session, headers)[0], 401)
        other = self.login("other")
        self.assertEqual(self.request(path, p, other, headers)[0], 401)
        code, value = self.request(path, p, self.session, headers)
        self.assertEqual(code, 200, value)
        self.assertEqual(self.request(path, p, self.session, headers)[0], 409)
        code, replay = self.request(path, p, self.session)
        self.assertEqual(code, 200, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["item"]["id"], value["item"]["id"])
        self.assertEqual(self.request(path, {**p, "title": "Changed text"}, self.session)[0], 409)

    def test_anonymous_browser_forged_legacy_and_logout_denied(self):
        p = topic()
        path = "/v1/matrix/topics"
        self.assertEqual(self.request(path, p)[0], 401)
        self.assertEqual(self.request(path, p, self.session, proof=False)[0], 403)
        self.assertEqual(self.request(path, p, self.session, {"Origin": cloud.SITE})[0], 403)
        self.assertEqual(
            self.request(path, p, self.session, {"Sec-Fetch-Site": "same-origin"})[0], 403
        )
        browser = self.login("fixture", "browser", False)
        self.assertEqual(self.request(path, p, browser)[0], 403)
        legacy = self.login("fixture", "desktop", False)
        self.assertEqual(self.request(path, p, legacy)[0], 403)
        self.assertEqual(self.request("/v1/auth/logout", {}, self.session)[0], 200)
        self.assertEqual(self.request(path, p, self.session)[0], 401)

    def test_native_key_binding_cannot_be_requested_from_browser(self):
        keys = matrix_proof.run({"action": "generate"})
        body = {
            "challenge": cloud.challenge(secrets.token_urlsafe(32)),
            "state": secrets.token_urlsafe(32),
            "scope": "desktop",
            "redirect_uri": cloud.CLOUD + "/auth/callback",
            "public_key": keys["publicKey"],
        }
        self.assertEqual(
            self.request("/v1/auth/start", body, headers={"Origin": cloud.SITE})[0], 403
        )

    def test_project_https_links_and_large_public_content_preserve_path_privacy(self):
        path = "/v1/matrix/topics"
        p = {
            **topic("project-public-https"),
            "type": "project",
            "projectUrl": "https://example.com/project",
            "content": "Public project reference https://example.com/docs. " * 190,
        }
        self.assertGreater(len(matrix_proof.encode(p)), 8192)
        code, value = self.request(path, p, self.session)
        self.assertEqual(code, 200, value)
        self.assertEqual(value["item"]["projectUrl"], p["projectUrl"])
        for index, private in enumerate(
            (
                r"C:\private\project",
                "c:/private/project",
                "/home/user/project",
                "/Users/user/project",
            )
        ):
            code, value = self.request(
                path,
                {
                    **p,
                    "requestId": "project-private-path-" + str(index),
                    "content": "Do not share " + private,
                },
                self.session,
            )
            self.assertEqual(code, 400, value)
            self.assertEqual(value["error"], "matrix_private_content_detected")

    def test_forum_privacy_roles_cas_and_withdraw(self):
        path = "/v1/matrix/topics"
        p = topic()
        for changes in (
            {"confirmed": False},
            {"content": "access_token=fixture-secret"},
            {"project_memory": "private"},
        ):
            self.assertEqual(self.request(path, {**p, **changes}, self.session)[0], 400)
        self.assertEqual(self.request(path, {**p, "type": "update"}, self.session)[0], 403)
        code, value = self.request(path, p, self.session)
        self.assertEqual(code, 200, value)
        tid = value["item"]["id"]
        change = path + "/" + tid + "/state"
        b = {
            "requestId": "state-test-1",
            "expectedRevision": 1,
            "publication": "public",
            "confirmed": True,
            "state": "resolved",
            "note": "Verified fixture recovery.",
        }
        self.assertEqual(self.request(change, b, self.session)[0], 403)
        with patch.dict("os.environ", {"CONTROL_MATRIX_ADMINS": "fixture"}):
            self.assertEqual(self.request(change, b, self.session)[0], 200)
            self.assertEqual(
                self.request(change, {**b, "requestId": "state-test-2"}, self.session)[0], 409
            )
        other = self.login("other")
        withdraw = {k: v for k, v in b.items() if k != "state"}
        withdraw.update(requestId="withdraw-test", expectedRevision=2)
        self.assertEqual(self.request(path + "/" + tid + "/withdraw", withdraw, other)[0], 403)
        self.assertEqual(
            self.request(path + "/" + tid + "/withdraw", withdraw, self.session)[0], 200
        )
        self.assertEqual(self.request(path + "/" + tid)[0], 404)
        self.assertEqual(self.request(path)[1]["items"], [])

    def test_public_read_search_reply_pagination_and_events(self):
        for i in range(22):
            code, result = self.request(
                "/v1/matrix/topics",
                {**topic("page-" + str(i)), "title": "Synthetic issue " + str(i)},
                self.session,
            )
            self.assertEqual(code, 200, result)
        code, first = self.request("/v1/matrix/topics")
        self.assertEqual(len(first["items"]), 20)
        self.assertTrue(first["hasMore"])
        last = self.request("/v1/matrix/topics?before=" + str(first["nextCursor"]))[1]
        self.assertEqual(len(last["items"]), 2)
        self.assertFalse(last["hasMore"])
        tid = last["items"][-1]["id"]
        reply = {
            "requestId": "reply-test",
            "publication": "public",
            "confirmed": True,
            "content": "Confirmed in an isolated fixture.",
        }
        self.assertEqual(
            self.request("/v1/matrix/topics/" + tid + "/replies", reply, self.session)[0], 200
        )
        self.assertEqual(len(self.request("/v1/matrix/topics/" + tid + "/replies")[1]["items"]), 1)
        self.assertEqual(len(self.request("/v1/matrix/topics?query=issue%200")[1]["items"]), 1)
        self.assertEqual(self.request("/v1/matrix/events")[0], 401)
        events = self.request("/v1/matrix/events?after=0", session=self.session)[1]
        self.assertEqual(len(events["events"]), 23)
        self.assertFalse(events["trustedInstructions"])
        self.assertEqual(self.request("/v1/matrix/events?after=999", session=self.session)[0], 409)


class GrowthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = control.Application({"coordination_mode": "local"}, Path(self.temp.name))
        self.peer = {"id": "fixture-peer", "project": "control", "actor_id": "fixture-actor"}
        self.calls = []

        def transport(path, body=None, token=None):
            self.calls.append((path, body, token))
            if path.startswith("/v1/matrix/events"):
                return {
                    "events": [
                        {
                            "seq": 1,
                            "topic": "00000000-0000-0000-0000-000000000001",
                            "kind": "created",
                        }
                    ],
                    "nextCursor": 1,
                    "hasMore": False,
                }
            return {"ok": True, "item": {"id": "00000000-0000-0000-0000-000000000001"}}

        self.app.cloud = CloudLink(transport)

    def login(self, subject):
        self.app.cloud.session = {
            "access_token": "fixture-" + subject,
            "expires_at": time.time() + 300,
            "scope": "desktop",
            "user": {"subject": subject, "name": subject},
        }

    def test_logged_out_is_local_and_cursor_scoped_persistent(self):
        self.assertFalse(self.app.growth.status(self.peer)["enabled"])
        with self.assertRaises(CloudError):
            self.app.growth.sync(self.peer, {"session_id": "fixture"})
        self.assertEqual(self.calls, [])
        self.login("one")
        self.app.growth.sync(self.peer, {"session_id": "fixture"})
        self.assertEqual(self.app.growth.status(self.peer)["observedCursor"], 1)
        self.login("two")
        self.assertEqual(self.app.growth.status(self.peer)["observedCursor"], 0)
        self.app.growth.sync(self.peer, {"session_id": "fixture"})
        self.assertTrue(self.calls[-1][0].endswith("after=0"))
        self.login("one")
        self.assertEqual(self.app.growth.status(self.peer)["observedCursor"], 1)
        self.app.cloud.logout()
        with self.assertRaises(CloudError):
            self.app.growth.sync(self.peer, {"session_id": "fixture"})

    def test_uncertain_publish_retains_id_and_account_no_false_receipt(self):
        self.login("one")
        p = {"session_id": "fixture", "action": "create", "topic": "", "payload": topic()}
        with patch.object(self.app.cloud, "call", side_effect=CloudError("cloud_unavailable", 503)):
            with self.assertRaises(CloudError):
                self.app.growth.publish(self.peer, p)
        self.assertEqual(self.app.growth.status(self.peer)["pendingPublicReceipts"], 1)
        self.login("two")
        self.assertEqual(self.app.growth.status(self.peer)["pendingPublicReceipts"], 0)
        self.login("one")
        r = self.app.growth.publish(self.peer, p)
        self.assertEqual(r["state"], "received")
        count = len(self.calls)
        self.assertTrue(self.app.growth.publish(self.peer, p)["replayed"])
        self.assertEqual(len(self.calls), count)
        with self.assertRaises(FeedbackError):
            self.app.growth.publish(
                self.peer, {**p, "payload": {**p["payload"], "title": "Changed"}}
            )

    def test_sync_failure_backoff_is_durable_and_account_scoped(self):
        self.login("one")
        with patch.object(
            self.app.cloud, "call", side_effect=CloudError("cloud_unavailable", 503)
        ) as send:
            with self.assertRaises(CloudError):
                self.app.growth.sync(self.peer, {"session_id": "fixture"})
            self.assertGreater(self.app.growth.status(self.peer)["nextSyncAttempt"], time.time())
            with self.assertRaises(CloudError) as caught:
                self.app.growth.sync(self.peer, {"session_id": "fixture"})
            self.assertEqual(caught.exception.code, "matrix_sync_backoff")
            self.assertEqual(send.call_count, 1)
        self.login("two")
        self.app.growth.sync(self.peer, {"session_id": "fixture"})
        self.login("one")
        with self.app.store.db() as db:
            db.execute("UPDATE matrix_sync_retry SET next_attempt=0 WHERE subject='one'")
        self.app.growth.sync(self.peer, {"session_id": "fixture"})
        self.assertEqual(self.app.growth.status(self.peer)["nextSyncAttempt"], 0)

    def test_growth_evidence_cas_and_candidate_continuity(self):
        body = {
            "session_id": "fixture",
            "request_id": "growth-one",
            "growthId": "reconnect",
            "expectedRevision": 0,
            "state": "observation",
            "baseline": "0.6.2",
            "candidateDigest": "",
            "source": "synthetic-fixture",
            "evidence": ["fixture-result.json"],
            "note": "Observed in isolated test.",
        }
        first = self.app.growth.record(self.peer, body)
        self.assertEqual(first["revision"], 1)
        self.assertTrue(self.app.growth.record(self.peer, body)["replayed"])
        self.assertFalse(first["deploymentVerified"])
        with self.assertRaises(FeedbackError):
            self.app.growth.record(
                self.peer,
                {
                    **body,
                    "request_id": "jump",
                    "expectedRevision": 1,
                    "state": "adopted",
                    "candidateDigest": "a" * 64,
                },
            )
        for rev, state in enumerate(
            ["proposal", "candidate", "verified", "canary", "adopted", "rolled_back"], 1
        ):
            body.update(
                request_id="stage-" + state,
                expectedRevision=rev,
                state=state,
                candidateDigest="" if state == "proposal" else "a" * 64,
            )
            result = self.app.growth.record(self.peer, body)
            self.assertEqual(result["revision"], rev + 1)
        self.assertFalse(result["automaticApply"])
        other = {**self.peer, "project": "other"}
        self.assertEqual(self.app.growth.status(other)["records"], [])


class MatrixNativeIntegrationTests(unittest.TestCase):
    """Real Agent HTTP -> local CloudLink -> signed cloud HTTP, synthetic accounts."""

    def setUp(self):
        self.agent = agent_fixture.AgentAccessTests()
        self.addCleanup(self.agent.doCleanups)
        self.agent.setUp()
        self.agent.connect()
        self.remote = MatrixHTTPTests()
        self.addCleanup(self.remote.doCleanups)
        self.remote.setUp()
        self.origin = patch.object(cloud_link, "ORIGIN", self.remote.base)
        self.origin.start()
        self.addCleanup(self.origin.stop)
        self.callbacks = patch.object(
            cloud, "CALLBACKS", cloud.CALLBACKS | {self.remote.base + "/auth/callback"}
        )
        self.callbacks.start()
        self.addCleanup(self.callbacks.stop)
        self.agent.app.cloud = CloudLink()

    def tool(self, name, **args):
        return agent_fixture.client_module.invoke(self.agent.client, "aieyra_" + name, args)

    def login(self, subject):
        link = self.agent.app.cloud
        link.start()
        self.remote.app.authorize(link.flow["flow_id"], subject)
        self.assertTrue(link.poll()["enabled"])

    def test_native_tools_login_publish_sync_switch_logout_and_growth(self):
        sid = self.agent.sid
        args = {"session_id": sid, "action": "create", "topic": "", "payload": topic()}
        with self.assertRaises(agent_fixture.client_module.ClientError):
            self.tool("matrix_publish", **args)
        self.login("native-one")
        self.assertTrue(
            self.tool("matrix_read", session_id=sid, view="status")["capabilities"]["participate"]
        )
        first = self.tool("matrix_publish", **args)
        self.assertEqual(first["state"], "received")
        tid = first["receipt"]["item"]["id"]
        filtered = self.tool("matrix_read", session_id=sid, view="topics", board="feedback")
        self.assertEqual([item["id"] for item in filtered["items"]], [tid])
        self.assertEqual(
            self.tool("matrix_read", session_id=sid, view="topics", board="lounge")["items"], []
        )
        with self.assertRaises(agent_fixture.client_module.ClientError):
            self.tool("matrix_read", session_id=sid, view="topics", board="invalid")
        with patch.object(self.agent.app.cloud, "call") as outbound:
            with self.assertRaises(agent_fixture.client_module.ClientError) as retired:
                self.agent.client.call("cloud/share", {"session_id": sid})
            self.assertEqual(retired.exception.status, 410)
            self.assertEqual(retired.exception.code, "community_write_retired_use_matrix")
            outbound.assert_not_called()
        self.assertTrue(self.tool("matrix_publish", **args)["replayed"])
        events = self.tool("matrix_sync", session_id=sid)
        self.assertEqual(len(events["events"]), 1)
        self.assertFalse(events["trustedInstructions"])
        self.assertEqual(self.tool("matrix_sync", session_id=sid)["events"], [])
        payload = {
            "session_id": sid,
            "request_id": "native-growth-one",
            "growthId": "native-fix",
            "expectedRevision": 0,
            "state": "observation",
            "baseline": "fixture-baseline",
            "candidateDigest": "",
            "source": tid,
            "evidence": ["synthetic-integration"],
            "note": "Verified native forum receipt.",
        }
        recorded = self.tool("growth_record", **payload)
        self.assertEqual(recorded["revision"], 1)
        self.assertFalse(recorded["deploymentVerified"])
        status = self.tool("growth_status")
        self.assertEqual(status["observedCursor"], 1)
        serialized = json.dumps(status)
        self.assertNotIn(self.agent.app.cloud.session["access_token"], serialized)
        self.assertNotIn("PRIVATE KEY", serialized)
        self.agent.app.cloud.logout()
        with self.assertRaises(agent_fixture.client_module.ClientError):
            self.tool("matrix_sync", session_id=sid)
        self.login("native-two")
        self.assertEqual(self.tool("growth_status")["observedCursor"], 0)
        second = self.tool("matrix_publish", **args)
        self.assertFalse(second["replayed"])
        self.assertNotEqual(second["receipt"]["item"]["id"], tid)
        with self.assertRaises(agent_fixture.client_module.ClientError):
            self.tool("matrix_publish", **{**args, "session_id": "foreign-session"})

    def test_same_device_proof_cannot_move_to_a_second_session(self):
        self.login("native-one")
        first = dict(self.agent.app.cloud.session)
        self.login("native-one")
        second = dict(self.agent.app.cloud.session)
        # Bind the same synthetic device key to two distinct account sessions.
        with self.remote.app.db() as db:
            db.execute(
                "UPDATE matrix_session_keys SET public_key=(SELECT public_key FROM matrix_session_keys WHERE session=?) WHERE session=?",
                (cloud.digest(first["access_token"]), cloud.digest(second["access_token"])),
            )
        second["matrix_private_key"] = first["matrix_private_key"]
        path, body = "/v1/matrix/topics", topic()
        proof = matrix_proof.headers(path, body, first)
        self.assertEqual(self.remote.request(path, body, second, proof)[0], 401)
        self.assertEqual(self.remote.request(path, body, second)[0], 200)
