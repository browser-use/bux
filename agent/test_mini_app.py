from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
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

        starter_cards = [card for card in first if str(card["source"]).startswith("miniapp-starter:")]
        self.assertEqual(len(starter_cards), len(self.app.STARTER_IDEAS))
        self.assertEqual(
            sorted(card["id"] for card in first if str(card["source"]).startswith("miniapp-starter:")),
            sorted(card["id"] for card in second if str(card["source"]).startswith("miniapp-starter:")),
        )
        self.assertTrue(starter_cards[0]["buttons"])
        self.assertEqual(starter_cards[0]["visual"]["kind"], "image")

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
        finally:
            server.shutdown()
            server.server_close()

    def test_start_dispatch_creates_worker_topic_by_default(self) -> None:
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

        result = self.app._start_agent_work(suggestion_id, {"id": 42, "first_name": "Magnus"})
        deadline = time.time() + 2
        while not runs and time.time() < deadline:
            time.sleep(0.02)

        self.assertTrue(result["started"])
        self.assertTrue(result["topic_created"])
        self.assertEqual(result["thread_id"], 777)
        self.assertEqual(calls[0][0], "createForumTopic")
        self.assertEqual(calls[1][0], "sendMessage")
        self.assertEqual(runs[0][0], (100, 777))
        self.assertIn("The user accepted this Mini App card", runs[0][1])
        self.assertIn("Card title: Start visible work", runs[0][1])
        self.assertIn("Action prompt:\nDo the work", runs[0][1])
        with self.agency_db.conn() as db:
            row = db.execute(
                "SELECT status, worker_topic_id FROM suggestions WHERE id = ?",
                (suggestion_id,),
            ).fetchone()
        self.assertEqual(row["status"], "accepted")
        self.assertEqual(row["worker_topic_id"], 777)

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
        self.assertEqual(bindings, [((100, 777), "codex")])
        self.assertEqual(runs[0][0], (100, 777))

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
