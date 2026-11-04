"""Tests for the 'Accept chat requests' setting (helper, mini app routes, bot button + request flow).

The database layer is replaced by a small in-memory fake, so no Postgres is needed.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

os.environ.update({
    "TOKEN": "123:abc", "DATABASE_URL": "postgresql://u:p@localhost/db", "CHANNEL_ID": "1",
    "BOT_USERNAME": "testbot", "ADMIN_ID": "1", "SECRET_KEY": "x",
    "PRIMARY_COLOR": "#112233", "SECONDARY_COLOR": "#445566", "CARD_BG_COLOR": "#778899",
    "BORDER_COLOR": "#aabbcc", "TEXT_COLOR": "#ddeeff",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402


class FakeDB:
    """Just enough of db_fetch_one / db_execute / db_update_user for the code under test."""

    def __init__(self):
        self.users = {}      # user_id -> dict
        self.requests = []   # dicts: sender_id, receiver_id, status
        self.blocks = []     # (blocker_id, blocked_id)

    def add_user(self, uid, accept=True, **extra):
        self.users[str(uid)] = {"user_id": str(uid), "accept_chat_requests": accept,
                                "anonymous_name": f"User{uid}", "avatar_emoji": None,
                                "notifications_enabled": True, "privacy_public": True,
                                "is_admin": False, "tg_username": None, "share_username": False,
                                "hide_aura": False, "hide_bio": False, "hide_follower_count": False,
                                "hide_role": False, **extra}

    def fetch_one(self, query, params=()):
        q = " ".join(query.split())
        if "FROM blocks" in q:
            return {"1": 1} if (str(params[0]), str(params[1])) in self.blocks else None
        if "FROM chat_requests" in q:
            if "sender_id = %s AND receiver_id = %s" in q and q.count("sender_id") == 1:
                a, b = map(str, params[:2])
                pairs = [(a, b)]
            else:
                a, b = str(params[0]), str(params[1])
                pairs = [(a, b), (b, a)]
            for r in reversed(self.requests):
                if (r["sender_id"], r["receiver_id"]) in pairs:
                    return {"status": r["status"], "timestamp": None}
            return None
        if "FROM users" in q:
            u = self.users.get(str(params[0]))
            if not u:
                return None
            out = dict(u)
            out["accepting"] = u["accept_chat_requests"] is not False
            if "COALESCE(accept_chat_requests, TRUE) AS accept_chat_requests" in q:
                out["accept_chat_requests"] = out["accepting"]
            return out
        return None

    def execute(self, query, params=(), **kw):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO chat_requests"):
            self.requests.append({"sender_id": str(params[0]), "receiver_id": str(params[1]), "status": "pending"})
        return None

    def update_user(self, user_id, **fields):
        self.users[str(user_id)].update(fields)
        return True


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(bot, "db_fetch_one", fake.fetch_one)
    monkeypatch.setattr(bot, "db_execute", fake.execute)
    monkeypatch.setattr(bot, "db_update_user", fake.update_user)

    async def fetch_one_async(q, p=()):
        return fake.fetch_one(q, p)

    async def execute_async(q, p=(), **kw):
        return fake.execute(q, p)

    async def update_async(uid, **f):
        return fake.update_user(uid, **f)

    monkeypatch.setattr(bot, "db_fetch_one_async", fetch_one_async)
    monkeypatch.setattr(bot, "db_execute_async", execute_async)
    monkeypatch.setattr(bot, "db_update_user_async", update_async)
    # the mini app route notifies the receiver over HTTP; never hit the network in tests
    posted = []
    monkeypatch.setattr(bot._tg_session, "post", lambda *a, **k: posted.append((a, k)))
    fake.posted = posted
    return fake


@pytest.fixture()
def client():
    return bot.flask_app.test_client()


# ---------------- helper ----------------

def test_helper_true_for_default_missing_user_and_null(db):
    db.add_user(1)
    db.add_user(2, accept=None)
    assert bot.user_accepts_chat_requests(1) is True
    assert bot.user_accepts_chat_requests(2) is True       # NULL column from old rows
    assert bot.user_accepts_chat_requests(999) is True     # unknown user never blocks


def test_helper_false_only_when_explicitly_off(db):
    db.add_user(1, accept=False)
    assert bot.user_accepts_chat_requests(1) is False


# ---------------- mini app settings routes ----------------

def test_settings_get_includes_flag(db, client):
    db.add_user(10, accept=False)
    body = client.get("/api/mini-app/settings/10").get_json()
    assert body["success"] and body["data"]["accept_chat_requests"] is False


def test_settings_post_saves_flag(db, client):
    db.add_user(10, accept=True)
    r = client.post("/api/mini-app/settings/10", json={"accept_chat_requests": False})
    assert r.status_code == 200 and r.get_json()["success"]
    assert db.users["10"]["accept_chat_requests"] is False
    client.post("/api/mini-app/settings/10", json={"accept_chat_requests": True})
    assert db.users["10"]["accept_chat_requests"] is True


def test_settings_post_rejects_non_boolean(db, client):
    db.add_user(10)
    r = client.post("/api/mini-app/settings/10", json={"accept_chat_requests": "no"})
    assert r.status_code == 400
    assert db.users["10"]["accept_chat_requests"] is True


# ---------------- mini app request route ----------------

def _send(client, s, r):
    return client.post("/api/mini-app/chat-request/send", json={"sender_id": s, "receiver_id": r})


def test_mini_app_request_refused_when_receiver_off(db, client):
    db.add_user(1)
    db.add_user(2, accept=False)
    r = _send(client, 1, 2)
    assert r.status_code == 403
    assert r.get_json()["error"] == bot.NOT_ACCEPTING_REQUESTS_MSG
    assert db.requests == [] and db.posted == []   # nothing stored, receiver not pinged


def test_mini_app_request_allowed_when_receiver_on(db, client):
    db.add_user(1)
    db.add_user(2)
    r = _send(client, 1, 2)
    assert r.status_code == 200 and r.get_json()["status"] == "pending"
    assert len(db.requests) == 1


def test_mini_app_existing_accepted_chat_still_works_when_off(db, client):
    db.add_user(1)
    db.add_user(2, accept=False)
    db.requests.append({"sender_id": "1", "receiver_id": "2", "status": "accepted"})
    r = _send(client, 1, 2)
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"


def test_status_endpoint_reports_accepting(db, client):
    db.add_user(1)
    db.add_user(2, accept=False)
    body = client.get("/api/mini-app/chat-request/status?user_id=1&target_id=2").get_json()
    assert body["status"] == "none" and body["accepting"] is False


# ---------------- bot: toggle button + request flow ----------------

class FakeQuery:
    def __init__(self, data, user_id=0):
        self.data = data
        self.from_user = SimpleNamespace(id=int(user_id))
        self.answers = []
        self.replies = []
        self.edits = []
        self.message = SimpleNamespace(reply_text=self._reply)

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))

    async def _reply(self, text, **kw):
        self.replies.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edits.append(reply_markup)


def _update(user_id, data):
    q = FakeQuery(data, user_id)
    return SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=int(user_id)),
                           effective_chat=SimpleNamespace(id=int(user_id)), effective_message=None), q


def _context():
    sent = []

    async def send_message(**kw):
        sent.append(kw)

    return SimpleNamespace(bot=SimpleNamespace(send_message=send_message), user_data={}, chat_data={}), sent


def test_privacy_menu_shows_accept_button(db):
    db.add_user(5, accept=False)
    update, q = _update(5, "privacy_settings")
    asyncio.run(bot.show_privacy_settings(update, SimpleNamespace(user_data={})))
    labels = {b.callback_data: b.text for row in q.edits[0].inline_keyboard for b in row}
    assert labels["toggle_accept_requests"] == "Accept Chat Requests: OFF"


def test_bot_toggle_flips_flag_both_ways(db):
    db.add_user(5, accept=True)
    for expected in (False, True):
        update, q = _update(5, "toggle_accept_requests")
        asyncio.run(bot.button_handler(update, _context()[0]))
        assert db.users["5"]["accept_chat_requests"] is expected


def test_bot_request_refused_when_receiver_off(db):
    db.add_user(1)
    db.add_user(2, accept=False)
    update, q = _update(1, "chatrequest_2")
    ctx, sent = _context()
    asyncio.run(bot.button_handler(update, ctx))
    assert (bot.NOT_ACCEPTING_REQUESTS_MSG, True) in q.answers
    assert db.requests == [] and sent == []


def test_bot_request_goes_through_when_receiver_on(db):
    db.add_user(1)
    db.add_user(2)
    update, q = _update(1, "chatrequest_2")
    ctx, sent = _context()
    asyncio.run(bot.button_handler(update, ctx))
    assert len(db.requests) == 1
    assert (bot.NOT_ACCEPTING_REQUESTS_MSG, True) not in q.answers
