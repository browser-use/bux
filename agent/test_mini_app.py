from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_DIR))


def _signed_init_data(token: str, user_id: int) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "test-query",
        "user": json.dumps(
            {"id": user_id, "first_name": "Magnus", "username": "Magnus_Mueller"},
            separators=(",", ":"),
        ),
    }
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(values)


class MiniAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["TG_BOT_TOKEN"] = "123456:test-token"
        os.environ["TG_OWNER_ID"] = "42"
        os.environ["BUX_AGENCY_DB"] = str(root / "agency.db")
        os.environ["BUX_MINIAPP_DB"] = str(root / "miniapp.db")
        os.environ["BUX_GOALS_FILE"] = str(root / "private" / "goals.md")
        os.environ["BUX_AGENCY_FEEDBACK_FILE"] = str(root / "private" / "feedback.md")
        os.environ["BUX_MINIAPP_SEED_STARTERS"] = "0"
        os.environ.pop("BUX_MINIAPP_DEV", None)
        for name in ("agency_db", "mini_app"):
            sys.modules.pop(name, None)
        self.agency_db = importlib.import_module("agency_db")
        self.app = importlib.import_module("mini_app")
        self.init_data = _signed_init_data(os.environ["TG_BOT_TOKEN"], 42)

    def tearDown(self) -> None:
        os.environ.pop("BUX_MINIAPP_SEED_STARTERS", None)
        os.environ.pop("BUX_GOALS_FILE", None)
        os.environ.pop("BUX_AGENCY_FEEDBACK_FILE", None)
        self.tmp.cleanup()

    def test_validate_init_data_rejects_wrong_owner(self) -> None:
        bad = _signed_init_data(os.environ["TG_BOT_TOKEN"], 7)
        with self.assertRaises(PermissionError):
            self.app._validate_init_data(bad)

    def test_dev_auth_disabled_when_public_url_is_set(self) -> None:
        os.environ["BUX_MINIAPP_DEV"] = "1"
        os.environ["BUX_MINIAPP_PUBLIC_URL"] = "https://example.com"
        try:
            with self.assertRaises(PermissionError):
                self.app._validate_init_data("dev")
        finally:
            os.environ.pop("BUX_MINIAPP_DEV", None)
            os.environ.pop("BUX_MINIAPP_PUBLIC_URL", None)

    def test_cards_keep_visible_copy_specific(self) -> None:
        with self.app._mini_conn() as db:
            db.execute(
                """
                INSERT INTO goals (title, context, cadence, created_at, updated_at)
                VALUES ('Ship the startup', 'Launch faster', '30 min', 1, 1)
                """
            )
            db.commit()
        with self.agency_db.conn() as db:
            self.agency_db.insert(
                db,
                title="Reply to investor",
                description="Keeps the round warm.",
                importance="high",
                source="gmail",
                prompt="Draft a clear reply",
            )

        cards = self.app._cards()

        self.assertEqual(cards[0]["title"], "Reply to investor")
        self.assertEqual(cards[0]["why"], "Keeps the round warm.")
        self.assertEqual(cards[0]["visual"]["kind"], "none")

    def test_cards_seed_starter_ideas_once(self) -> None:
        os.environ.pop("BUX_MINIAPP_SEED_STARTERS", None)

        first = self.app._cards()
        second = self.app._cards()

        starter_cards = [card for card in first if str(card["source"]).startswith("miniapp-")]
        self.assertGreaterEqual(len(self.app.STARTER_IDEAS), 10)
        self.assertEqual(len(starter_cards), len(self.app.STARTER_IDEAS))
        self.assertEqual(
            sorted(card["id"] for card in first if str(card["source"]).startswith("miniapp-")),
            sorted(card["id"] for card in second if str(card["source"]).startswith("miniapp-")),
        )
        self.assertTrue(starter_cards[0]["buttons"])
        self.assertGreater(len(starter_cards[0]["buttons"]), 1)
        self.assertTrue(starter_cards[0]["blocks"])
        self.assertEqual(starter_cards[0]["visual"]["kind"], "none")
        self.assertTrue(all(len(card["buttons"]) >= 2 for card in starter_cards))
        self.assertTrue(all(card["source_label"] == "Starter goal" for card in starter_cards))

    def test_cards_include_local_image_data_url(self) -> None:
        image_path = Path(self.tmp.name) / "card.png"
        image_path.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
            )
        )
        with self.agency_db.conn() as db:
            self.agency_db.insert(
                db,
                title="Show image",
                description="Visual proof.",
                importance="med",
                source="gmail",
                source_label="Gmail thread",
                source_url="https://mail.google.com/mail/u/0/#inbox/abc",
                image_file=str(image_path),
            )

        cards = self.app._cards()

        self.assertEqual(cards[0]["visual"]["kind"], "image")
        self.assertTrue(cards[0]["visual"]["src"].startswith("data:image/png;base64,"))
        self.assertEqual(cards[0]["source_label"], "Gmail thread")
        self.assertEqual(cards[0]["source_url"], "https://mail.google.com/mail/u/0/#inbox/abc")

    def test_cards_include_expandable_blocks(self) -> None:
        with self.agency_db.conn() as db:
            self.agency_db.insert(
                db,
                title="Reply with variants",
                description="",
                importance="high",
                source="gmail",
                buttons=["Send A", "Send B"],
                blocks=[
                    {"emoji": "A", "title": "Variant A", "body_html": "<pre>book a call</pre>"},
                    {"emoji": "B", "title": "Variant B", "body_html": "loop in <b>team</b>"},
                ],
            )

        cards = self.app._cards()

        self.assertEqual(cards[0]["blocks"][0]["title"], "Variant A")
        self.assertEqual(cards[0]["blocks"][0]["body"], "book a call")
        self.assertEqual(cards[0]["blocks"][1]["body"], "loop in team")

    def test_http_goal_and_cards_flow(self) -> None:
        with self.agency_db.conn() as db:
            suggestion_id = self.agency_db.insert(
                db,
                title="Review pull request",
                description="Unblocks a deploy.",
                importance="med",
                source="github",
                prompt="Review and merge if clean",
            )
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.app.MiniAppHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            self._request(
                base + "/api/goals",
                method="POST",
                body={"title": "Win", "context": "Get more users", "cadence": "hourly"},
            )
            goals_text = Path(os.environ["BUX_GOALS_FILE"]).read_text()
            self.assertIn("## Win", goals_text)
            self.assertIn("Get more users", goals_text)
            self.assertIn("Cadence: hourly", goals_text)
            cards = self._request(base + "/api/cards")
            self.assertEqual(cards["cards"][0]["id"], suggestion_id)
            self._request(
                f"{base}/api/cards/{suggestion_id}/comment",
                method="POST",
                body={"comment": "Make it tighter"},
            )
            self._request(f"{base}/api/cards/{suggestion_id}/dismiss", method="POST", body={})
            with self.agency_db.conn() as db:
                row = db.execute("SELECT status FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
            self.assertEqual(row["status"], "dismissed")
            feedback = Path(os.environ["BUX_AGENCY_FEEDBACK_FILE"]).read_text()
            self.assertIn("Review pull request", feedback)
            self.assertIn("do not re-pitch", feedback)
        finally:
            server.shutdown()
            server.server_close()

    def test_concept_routes_serve_ten_goal_game_shells(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.app.MiniAppHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            for route in ["/miniapp", "/mini-apps", "/mini-app-1", "/mini-app-5", "/mini-app-10", "/miniapp/7"]:
                with urllib.request.urlopen(base + route, timeout=5) as res:
                    body = res.read().decode()
                    content_type = res.headers.get("Content-Type", "")
                self.assertIn("text/html", content_type)
                self.assertIn("agency prototypes", body)
                self.assertIn("Ten gamified ways", body)
                self.assertIn("/concepts.js", body)

            for asset in ["/concepts.css", "/concepts.js"]:
                with urllib.request.urlopen(base + asset, timeout=5) as res:
                    self.assertEqual(res.status, 200)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/mini-app-11", timeout=5)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()

    def test_activity_endpoint_returns_recent_resolutions(self) -> None:
        with self.agency_db.conn() as db:
            accepted_id = self.agency_db.insert(
                db,
                title="Ship the release",
                description="Release candidate is ready.",
                importance="high",
                source="github-release",
                source_label="GitHub",
            )
            dismissed_id = self.agency_db.insert(
                db,
                title="Monitor generic Slack chatter",
                description="Too vague.",
                importance="low",
                source="slack",
                source_label="Slack",
            )
            self.agency_db.set_status(db, accepted_id, "accepted")
            self.agency_db.set_status(db, dismissed_id, "dismissed")
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.app.MiniAppHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            activity = self._request(base + "/api/activity")
            self.assertEqual(activity["activity"][0]["status"], "dismissed")
            self.assertEqual(activity["activity"][1]["status"], "accepted")
            self.assertEqual(activity["activity"][1]["title"], "Ship the release")
        finally:
            server.shutdown()
            server.server_close()

    def test_topic_generate_prompt_includes_decision_history(self) -> None:
        with self.agency_db.conn() as db:
            accepted_id = self.agency_db.insert(
                db,
                title="Make New Goal a one-tap goal picker in tinder.html",
                description="Goal creation should be fast.",
                importance="high",
                source="goal-ui",
                source_label="tinder.html",
                thread_id=4292,
                buttons=["Yes"],
            )
            dismissed_id = self.agency_db.insert(
                db,
                title="Start generic Slack monitor",
                description="Too vague.",
                importance="med",
                source="slack-monitor",
                source_label="Starter idea",
                thread_id=4292,
                buttons=["Skip"],
            )
            self.agency_db.set_status(db, accepted_id, "accepted")
            self.agency_db.set_status(db, dismissed_id, "dismissed")

        prompt = self.app._topic_generate_prompt(4292, "Agency UI")

        self.assertIn("Recent tap history:", prompt)
        self.assertIn("accepted/completed:", prompt)
        self.assertIn("Make New Goal a one-tap goal picker", prompt)
        self.assertIn("dismissed:", prompt)
        self.assertIn("Start generic Slack monitor", prompt)

    def test_start_dispatch_uses_existing_goal_topic(self) -> None:
        calls: list[tuple[str, dict]] = []
        runs: list[tuple[tuple[int, int], str]] = []

        class FakeBot:
            def __init__(self, token: str, setup_token: str) -> None:
                self.token = token
                self.setup_token = setup_token

            def call(self, method: str, **params: object) -> dict:
                calls.append((method, dict(params)))
                if method == "createForumTopic":
                    return {"ok": True, "result": {"message_thread_id": 777}}
                return {"ok": True, "result": {"message_id": 55}}

            def run_task(
                self,
                key: tuple[int, int],
                prompt: str,
                reply_to: int | None = None,
                sender: dict | None = None,
            ) -> None:
                del reply_to, sender
                runs.append((key, prompt))

        sys.modules["telegram_bot"] = types.SimpleNamespace(Bot=FakeBot)
        with self.agency_db.conn() as db:
            suggestion_id = self.agency_db.insert(
                db,
                title="Start visible work",
                description="",
                chat_id=100,
                thread_id=123,
                prompt="Do the work",
            )
            self.agency_db.update_message(db, suggestion_id, 987)

        result = self.app._start_agent_work(suggestion_id, {"id": 42, "first_name": "Magnus"})
        deadline = time.time() + 2
        while not runs and time.time() < deadline:
            time.sleep(0.02)

        self.assertTrue(result["started"])
        self.assertFalse(result["topic_created"])
        self.assertEqual(result["thread_id"], 123)
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(runs[0][0], (100, 123))
        self.assertIn("The user accepted this Mini App card", runs[0][1])
        self.assertIn("Card title: Start visible work", runs[0][1])
        self.assertIn("Action prompt:\nDo the work", runs[0][1])
        with self.agency_db.conn() as db:
            row = db.execute(
                "SELECT status, worker_topic_id FROM suggestions WHERE id = ?",
                (suggestion_id,),
            ).fetchone()
        self.assertEqual(row["status"], "accepted")
        self.assertEqual(row["worker_topic_id"], 123)

    def test_start_dispatch_creates_topic_when_card_has_no_topic(self) -> None:
        calls: list[tuple[str, dict]] = []
        runs: list[tuple[tuple[int, int], str]] = []

        class FakeBot:
            def __init__(self, token: str, setup_token: str) -> None:
                self.token = token
                self.setup_token = setup_token

            def call(self, method: str, **params: object) -> dict:
                calls.append((method, dict(params)))
                if method == "createForumTopic":
                    return {"ok": True, "result": {"message_thread_id": 777}}
                return {"ok": True, "result": {"message_id": 55}}

            def run_task(
                self,
                key: tuple[int, int],
                prompt: str,
                reply_to: int | None = None,
                sender: dict | None = None,
            ) -> None:
                del reply_to, sender, prompt
                runs.append((key, ""))

        sys.modules["telegram_bot"] = types.SimpleNamespace(Bot=FakeBot)
        with self.agency_db.conn() as db:
            suggestion_id = self.agency_db.insert(
                db,
                title="Start new lane",
                description="",
                chat_id=100,
                thread_id=0,
                prompt="Do the work",
            )

        result = self.app._start_agent_work(suggestion_id, {"id": 42, "first_name": "Magnus"})
        deadline = time.time() + 2
        while not runs and time.time() < deadline:
            time.sleep(0.02)

        self.assertTrue(result["started"])
        self.assertTrue(result["topic_created"])
        self.assertEqual(result["thread_id"], 777)
        self.assertEqual(calls[0][0], "createForumTopic")
        self.assertEqual(runs[0][0], (100, 777))

    def test_dismiss_api_deletes_original_telegram_card_for_sync(self) -> None:
        calls: list[tuple[str, dict]] = []

        class FakeBot:
            def __init__(self, token: str, setup_token: str) -> None:
                self.token = token
                self.setup_token = setup_token

            def call(self, method: str, **params: object) -> dict:
                calls.append((method, dict(params)))
                return {"ok": True, "result": {}}

        sys.modules["telegram_bot"] = types.SimpleNamespace(Bot=FakeBot)
        with self.agency_db.conn() as db:
            suggestion_id = self.agency_db.insert(
                db,
                title="Dismiss me",
                description="",
                chat_id=100,
                thread_id=123,
                prompt="Do the work",
            )
            self.agency_db.update_message(db, suggestion_id, 456)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.app.MiniAppHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            result = self._request(f"{base}/api/cards/{suggestion_id}/dismiss", method="POST", body={})
            self.assertTrue(result["ok"])
            self.assertTrue(result["synced"])
            self.assertIn(("deleteMessage", {"chat_id": 100, "message_id": 456}), calls)
        finally:
            server.shutdown()
            server.server_close()

    def test_comment_api_marks_card_differently_so_it_does_not_reappear(self) -> None:
        with self.agency_db.conn() as db:
            suggestion_id = self.agency_db.insert(
                db,
                title="Refine me",
                description="",
                chat_id=100,
                thread_id=123,
                prompt="Do the work",
            )
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.app.MiniAppHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            result = self._request(
                f"{base}/api/cards/{suggestion_id}/comment",
                method="POST",
                body={"comment": "Make it sharper"},
            )
            self.assertTrue(result["ok"])
            cards = self._request(base + "/api/cards")
            self.assertEqual(cards["cards"], [])
            activity = self._request(base + "/api/activity")
            self.assertEqual(activity["activity"][0]["status"], "differently")
            with self.agency_db.conn() as db:
                row = db.execute("SELECT status FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
            self.assertEqual(row["status"], "differently")
        finally:
            server.shutdown()
            server.server_close()

    def test_start_dispatch_applies_miniapp_provider_setting(self) -> None:
        runs: list[tuple[tuple[int, int], str]] = []
        bindings: list[tuple[tuple[int, int], str]] = []

        class FakeBot:
            def __init__(self, token: str, setup_token: str) -> None:
                self.token = token
                self.setup_token = setup_token
                self.state = {"agents": {}}

            def call(self, method: str, **params: object) -> dict:
                if method == "createForumTopic":
                    return {"ok": True, "result": {"message_thread_id": 777}}
                return {"ok": True, "result": {"message_id": 55}}

            def run_task(
                self,
                key: tuple[int, int],
                prompt: str,
                reply_to: int | None = None,
                sender: dict | None = None,
            ) -> None:
                del reply_to, sender
                runs.append((key, prompt))

        def fake_set_agent_for(key: tuple[int, int], provider: str, state: dict) -> None:
            state.setdefault("agents", {})[f"{key[0]}_{key[1]}"] = provider
            bindings.append((key, provider))

        sys.modules["telegram_bot"] = types.SimpleNamespace(
            Bot=FakeBot,
            _set_agent_for=fake_set_agent_for,
        )
        self.app._write_setting("provider", "codex", {"id": 42})
        with self.agency_db.conn() as db:
            suggestion_id = self.agency_db.insert(
                db,
                title="Generate with Codex",
                description="",
                chat_id=100,
                thread_id=123,
                prompt="Do the work",
            )

        result = self.app._start_agent_work(suggestion_id, {"id": 42, "first_name": "Magnus"})
        deadline = time.time() + 2
        while not runs and time.time() < deadline:
            time.sleep(0.02)

        self.assertTrue(result["started"])
        self.assertEqual(bindings, [((100, 123), "codex")])
        self.assertEqual(runs[0][0], (100, 123))

    def test_start_dispatch_custom_button_includes_card_context(self) -> None:
        calls: list[tuple[str, dict]] = []
        runs: list[tuple[tuple[int, int], str]] = []

        class FakeBot:
            def __init__(self, token: str, setup_token: str) -> None:
                self.token = token
                self.setup_token = setup_token

            def call(self, method: str, **params: object) -> dict:
                calls.append((method, dict(params)))
                if method == "createForumTopic":
                    return {"ok": True, "result": {"message_thread_id": 777}}
                return {"ok": True, "result": {"message_id": 55}}

            def run_task(
                self,
                key: tuple[int, int],
                prompt: str,
                reply_to: int | None = None,
                sender: dict | None = None,
            ) -> None:
                del reply_to, sender
                runs.append((key, prompt))

        sys.modules["telegram_bot"] = types.SimpleNamespace(Bot=FakeBot)
        with self.agency_db.conn() as db:
            suggestion_id = self.agency_db.insert(
                db,
                title="Repost Saurav's n8n launch",
                description="Slack signal: Saurav asked for reposts.",
                source="slack-wall-channel-teammate-direct-repost-ask",
                chat_id=100,
                thread_id=0,
                buttons=["🟢 QT - A1 default", "✏️ Show 3 variants", "❌ Skip"],
            )

        result = self.app._start_agent_work(
            suggestion_id,
            {"id": 42, "first_name": "Magnus"},
            "✏️ Show 3 variants",
        )
        deadline = time.time() + 2
        while not runs and time.time() < deadline:
            time.sleep(0.02)

        self.assertTrue(result["started"])
        prompt = runs[0][1]
        self.assertIn("[agency-button] ✏️ Show 3 variants", prompt)
        self.assertIn("Title: Repost Saurav's n8n launch", prompt)
        self.assertIn("Source: slack-wall-channel-teammate-direct-repost-ask", prompt)
        self.assertIn("Slack signal: Saurav asked for reposts.", prompt)
        self.assertIn("find the matching entry by source or title", prompt)

    def test_game_state_tracks_rank_points_from_durable_tables(self) -> None:
        with self.agency_db.conn() as db:
            accepted_id = self.agency_db.insert(
                db,
                title="Accepted win",
                description="Moves the goal.",
                importance="high",
                source="gmail-thread",
            )
            dismissed_id = self.agency_db.insert(
                db,
                title="Skipped noise",
                description="Too vague.",
                importance="low",
                source="slack-noise",
            )
            self.agency_db.set_status(db, accepted_id, "accepted")
            self.agency_db.set_status(db, dismissed_id, "dismissed")
        with self.app._mini_conn() as db:
            db.execute(
                """
                INSERT INTO goals (title, context, created_at, updated_at)
                VALUES ('Grow', 'Reach users', 1, 1)
                """
            )
            db.execute(
                """
                INSERT INTO card_comments (suggestion_id, body, telegram_user_id, created_at)
                VALUES (?, 'Make it sharper', '42', 1)
                """,
                (accepted_id,),
            )
            db.commit()

        state = self.app._game_state()

        self.assertGreaterEqual(state["points"], 180 + 45 + 90 - 6)
        self.assertEqual(state["stats"]["done"], 1)
        self.assertEqual(state["stats"]["comments"], 1)
        self.assertIn("name", state["rank"])

    def test_game_state_prefers_quest_event_ledger(self) -> None:
        with self.agency_db.conn() as db:
            suggestion_id = self.agency_db.insert(
                db,
                title="High-value win",
                description="Moves the goal.",
                importance="high",
                source="launch",
            )
            self.agency_db.set_status(db, suggestion_id, "accepted")
        self.app._record_quest_event(
            suggestion_id,
            "start",
            {"id": 42},
            points=220,
            detail="Draft launch post",
        )

        state = self.app._game_state()

        self.assertEqual(state["points"], 220)
        self.assertEqual(state["recent_events"][0]["points"], 220)

    def test_agentcard_wallet_ledger_hold_cancel_and_settle(self) -> None:
        with self.agency_db.conn() as db:
            wallet = self.agency_db.agentcard_create_wallet(
                db,
                name="Ops test wallet",
                owner_user_id="42",
                created_by="42",
            )
            wallet = self.agency_db.agentcard_fund_wallet(
                db,
                wallet["id"],
                amount_cents=10_000,
                idempotency_key="grant-1",
            )
            wallet = self.agency_db.agentcard_fund_wallet(
                db,
                wallet["id"],
                amount_cents=10_000,
                idempotency_key="grant-1",
            )
            self.assertEqual(wallet["available_cents"], 10_000)

            reservation = self.agency_db.agentcard_reserve_wallet(
                db,
                wallet["id"],
                amount_cents=3_500,
                run_id="run-1",
                prompt="Buy one tool subscription",
            )
            duplicate = self.agency_db.agentcard_reserve_wallet(
                db,
                wallet["id"],
                amount_cents=3_500,
                run_id="run-1",
            )
            self.assertEqual(duplicate["id"], reservation["id"])
            self.assertEqual(reservation["wallet"]["available_cents"], 6_500)
            self.assertEqual(reservation["wallet"]["active_hold_cents"], 3_500)

            card = self.agency_db.agentcard_request_card(
                db,
                reservation["id"],
                requested_amount_cents=3_000,
                merchant="cursor.com",
            )
            self.assertEqual(card["relay_status"], "provider_not_configured")
            self.assertFalse(card["card_details_available"])
            self.assertNotIn("provider_card_id", card)

            settled = self.agency_db.agentcard_settle_reservation(
                db,
                reservation["id"],
                captured_amount_cents=2_240,
                provider_event_id="evt-1",
            )
            self.assertEqual(settled["status"], "settled")
            self.assertEqual(settled["wallet"]["available_cents"], 7_760)
            self.assertEqual(settled["wallet"]["active_hold_cents"], 0)

            second = self.agency_db.agentcard_reserve_wallet(
                db,
                wallet["id"],
                amount_cents=1_000,
                run_id="run-2",
            )
            cancelled = self.agency_db.agentcard_cancel_reservation(db, second["id"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(cancelled["wallet"]["available_cents"], 7_760)

    def test_agentcard_wallet_http_flow(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.app.MiniAppHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            created = self._request(
                base + "/api/agentcard/wallets",
                method="POST",
                body={"name": "Browser Use test wallet"},
            )
            wallet_id = created["wallet"]["id"]
            self.assertEqual(created["wallet"]["owner_user_id"], "42")

            funded = self._request(
                f"{base}/api/agentcard/wallets/{wallet_id}/fund",
                method="POST",
                body={"amount": "200.00", "idempotency_key": "local-grant"},
            )
            self.assertEqual(funded["wallet"]["available_cents"], 20_000)

            reserved = self._request(
                f"{base}/api/agentcard/wallets/{wallet_id}/reservations",
                method="POST",
                body={
                    "amount": "35.00",
                    "run_id": "local-run-1",
                    "prompt": "Buy a SaaS trial, max $35",
                },
            )
            reservation_id = reserved["reservation"]["id"]
            self.assertEqual(reserved["reservation"]["wallet"]["available_cents"], 16_500)
            self.assertEqual(reserved["reservation"]["remaining_hold_cents"], 3_500)

            card = self._request(
                f"{base}/api/agentcard/reservations/{reservation_id}/cards",
                method="POST",
                body={"merchant": "cursor.com", "amount": "30.00"},
            )
            self.assertFalse(card["provider_configured"])
            self.assertFalse(card["card"]["card_details_available"])
            self.assertEqual(card["card"]["relay_status"], "provider_not_configured")

            cards = self._request(f"{base}/api/agentcard/wallets/{wallet_id}/cards")
            self.assertEqual(len(cards["cards"]), 1)
            self.assertNotIn("provider_card_id", cards["cards"][0])

            cancelled = self._request(
                f"{base}/api/agentcard/reservations/{reservation_id}/cancel",
                method="POST",
                body={},
            )
            self.assertEqual(cancelled["reservation"]["status"], "cancelled")
            self.assertEqual(cancelled["reservation"]["wallet"]["available_cents"], 20_000)
        finally:
            server.shutdown()
            server.server_close()

    def test_agentcard_wallet_controls_are_in_static_shell(self) -> None:
        html = (AGENT_DIR / "mini_app_static" / "tinder.html").read_text()
        js = (AGENT_DIR / "mini_app_static" / "tinder.js").read_text()
        css = (AGENT_DIR / "mini_app_static" / "tinder.css").read_text()

        self.assertIn('id="walletPanel"', html)
        self.assertIn('id="startSheet"', html)
        self.assertIn('id="allowSpendInput"', html)
        self.assertIn("/api/agentcard/wallets", js)
        self.assertIn("payload.agentcard", js)
        self.assertIn("allow wallet spend", html.lower())
        self.assertIn(".wallet-panel", css)
        self.assertIn(".spend-controls", css)

    def test_bux_card_cli_requests_redacted_provider_stub(self) -> None:
        with self.agency_db.conn() as db:
            wallet = self.agency_db.agentcard_create_wallet(
                db,
                name="CLI wallet",
                owner_user_id="42",
                created_by="42",
            )
            self.agency_db.agentcard_fund_wallet(db, wallet["id"], amount_cents=5_000)
            reservation = self.agency_db.agentcard_reserve_wallet(
                db,
                wallet["id"],
                amount_cents=2_000,
                run_id="cli-run",
            )

        result = subprocess.run(
            [
                sys.executable,
                str(AGENT_DIR / "bux-card"),
                "request",
                "--reservation-id",
                str(reservation["id"]),
                "--amount",
                "12.50",
                "--merchant",
                "cursor.com",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["provider_configured"])
        self.assertEqual(payload["card"]["relay_status"], "provider_not_configured")
        self.assertFalse(payload["card"]["card_details_available"])
        self.assertNotIn("provider_card_id", payload["card"])

    def test_start_dispatch_creates_agentcard_reservation(self) -> None:
        calls: list[tuple[str, dict]] = []
        runs: list[tuple[tuple[int, int], str]] = []

        class FakeBot:
            def __init__(self, token: str, setup_token: str) -> None:
                self.token = token
                self.setup_token = setup_token

            def call(self, method: str, **params: object) -> dict:
                calls.append((method, dict(params)))
                return {"ok": True, "result": {"message_id": 55}}

            def run_task(
                self,
                key: tuple[int, int],
                prompt: str,
                reply_to: int | None = None,
                sender: dict | None = None,
            ) -> None:
                del reply_to, sender
                runs.append((key, prompt))

        sys.modules["telegram_bot"] = types.SimpleNamespace(Bot=FakeBot)
        with self.agency_db.conn() as db:
            wallet = self.agency_db.agentcard_create_wallet(
                db,
                name="Spend wallet",
                owner_user_id="42",
                created_by="42",
            )
            self.agency_db.agentcard_fund_wallet(db, wallet["id"], amount_cents=20_000)
            suggestion_id = self.agency_db.insert(
                db,
                title="Buy a SaaS trial",
                description="Use the controlled wallet.",
                chat_id=100,
                thread_id=123,
                prompt="Buy a SaaS trial if it is under budget.",
            )
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.app.MiniAppHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            result = self._request(
                f"{base}/api/cards/{suggestion_id}/start",
                method="POST",
                body={
                    "button": "Do it",
                    "agentcard": {
                        "enabled": True,
                        "wallet_id": wallet["id"],
                        "amount_cents": 3_500,
                    },
                },
            )
            deadline = time.time() + 2
            while not runs and time.time() < deadline:
                time.sleep(0.02)

            self.assertTrue(result["ok"])
            self.assertTrue(result["agentcard"]["enabled"])
            reservation = result["agentcard"]["reservation"]
            self.assertEqual(reservation["max_amount_cents"], 3_500)
            self.assertEqual(reservation["wallet"]["available_cents"], 16_500)
            self.assertIn("AgentCard wallet reservation", runs[0][1])
            self.assertIn("/opt/bux/repo/agent/bux-card request --reservation-id", runs[0][1])
            self.assertIn(str(reservation["id"]), runs[0][1])
        finally:
            server.shutdown()
            server.server_close()

    def test_start_dispatch_is_idempotent_after_acceptance(self) -> None:
        calls: list[tuple[str, dict]] = []
        runs: list[tuple[tuple[int, int], str]] = []

        class FakeBot:
            def __init__(self, token: str, setup_token: str) -> None:
                self.token = token
                self.setup_token = setup_token

            def call(self, method: str, **params: object) -> dict:
                calls.append((method, dict(params)))
                return {"ok": True, "result": {"message_id": 55}}

            def run_task(
                self,
                key: tuple[int, int],
                prompt: str,
                reply_to: int | None = None,
                sender: dict | None = None,
            ) -> None:
                del reply_to, sender
                runs.append((key, prompt))

        sys.modules["telegram_bot"] = types.SimpleNamespace(Bot=FakeBot)
        with self.agency_db.conn() as db:
            suggestion_id = self.agency_db.insert(
                db,
                title="Start once",
                description="",
                chat_id=100,
                thread_id=123,
                prompt="Do the work",
            )

        first = self.app._start_agent_work(suggestion_id, {"id": 42, "first_name": "Magnus"})
        deadline = time.time() + 2
        while not runs and time.time() < deadline:
            time.sleep(0.02)
        second = self.app._start_agent_work(suggestion_id, {"id": 42, "first_name": "Magnus"})

        self.assertTrue(first["started"])
        self.assertTrue(second["already_handled"])
        self.assertEqual(second["thread_id"], 123)
        self.assertEqual(len(runs), 1)

    def test_start_dispatch_claims_pending_before_side_effects(self) -> None:
        calls: list[tuple[str, dict]] = []

        class FakeBot:
            def __init__(self, token: str, setup_token: str) -> None:
                self.token = token
                self.setup_token = setup_token

            def call(self, method: str, **params: object) -> dict:
                calls.append((method, dict(params)))
                raise RuntimeError("telegram down")

            def run_task(
                self,
                key: tuple[int, int],
                prompt: str,
                reply_to: int | None = None,
                sender: dict | None = None,
            ) -> None:
                del key, prompt, reply_to, sender

        sys.modules["telegram_bot"] = types.SimpleNamespace(Bot=FakeBot)
        with self.agency_db.conn() as db:
            suggestion_id = self.agency_db.insert(
                db,
                title="Start atomically",
                description="",
                chat_id=100,
                thread_id=123,
                prompt="Do the work",
            )

        result = self.app._start_agent_work(suggestion_id, {"id": 42, "first_name": "Magnus"})

        self.assertFalse(result["started"])
        self.assertTrue(calls)
        with self.agency_db.conn() as db:
            row = db.execute("SELECT status FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        self.assertEqual(row["status"], "failed")

    def _request(self, url: str, *, method: str = "GET", body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Init-Data": self.init_data,
            },
        )
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read().decode())


if __name__ == "__main__":
    unittest.main()
