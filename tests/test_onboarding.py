"""Tests for the first-run questionnaire: sex (mandatory) -> nickname (skippable), then the deep link
the user opened the bot with (e.g. view comments) is replayed. In-memory fakes, no Postgres/Telegram."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

os.environ.update({
    "TOKEN": "123:abc", "DATABASE_URL": "postgresql://u:p@localhost/db", "CHANNEL_ID": "1",
    "BOT_USERNAME": "testbot", "ADMIN_ID": "1", "SECRET_KEY": "x",
    "PRIMARY_COLOR": "#112233", "SECONDARY_COLOR": "#445566", "CARD_BG_COLOR": "#778899",
    "BORDER_COLOR": "#aabbcc", "TEXT_COLOR": "#ddeeff",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class World:
    """Users table + everything the handlers send."""

    def __init__(self):
        self.users = {}
        self.sent = []            # (text, reply_markup)
        self.comments_shown = []  # post ids passed to show_comments_menu

    def add(self, uid, sex='👤', done=False, is_admin=False, name='Anonymous'):
        self.users[str(uid)] = {'user_id': str(uid), 'sex': sex, 'onboarding_done': done,
                                'is_admin': is_admin, 'anonymous_name': name, 'is_banned': False}


@pytest.fixture()
def world(monkeypatch):
    w = World()

    async def fetch_one_async(q, p=()):
        if "FROM users WHERE user_id" in q:
            u = w.users.get(str(p[0]))
            return dict(u) if u else None
        return None

    async def execute_async(q, p=(), **kw):
        if q.startswith("INSERT INTO users"):
            w.add(p[0], sex=p[2], is_admin=bool(p[3]), name=p[1])
            return True
        return None

    async def update_async(uid, **f):
        w.users[str(uid)].update(f)
        return True

    async def show_comments_menu(update, context, post_id, page=1, force_reveal=False, auto_show_comments=False):
        w.comments_shown.append(post_id)

    monkeypatch.setattr(bot, "db_fetch_one_async", fetch_one_async)
    monkeypatch.setattr(bot, "db_execute_async", execute_async)
    monkeypatch.setattr(bot, "db_update_user_async", update_async)
    monkeypatch.setattr(bot, "get_user_cached", lambda uid: dict(w.users[str(uid)]) if str(uid) in w.users else None)
    monkeypatch.setattr(bot, "get_main_menu", lambda uid: "MAIN_MENU")
    monkeypatch.setattr(bot, "show_comments_menu", show_comments_menu)
    return w


def _msg(world, text):
    async def reply_text(t, reply_markup=None, **kw):
        world.sent.append((t, reply_markup))
    return SimpleNamespace(text=text, reply_text=reply_text)


def text_update(world, uid, text):
    m = _msg(world, text)
    return SimpleNamespace(message=m, callback_query=None, effective_message=m,
                           effective_user=SimpleNamespace(id=int(uid)), effective_chat=SimpleNamespace(id=int(uid)))


def tap_update(world, uid, data):
    m = _msg(world, None)
    edits = []

    async def answer(text=None, show_alert=False, **kw):
        world.sent.append((f"[answer] {text}", show_alert))

    async def edit_message_text(t, **kw):
        edits.append(t)

    q = SimpleNamespace(data=data, message=m, answer=answer, edit_message_text=edit_message_text,
                        from_user=SimpleNamespace(id=int(uid)))
    u = SimpleNamespace(message=None, callback_query=q, effective_message=m,
                        effective_user=SimpleNamespace(id=int(uid)), effective_chat=SimpleNamespace(id=int(uid)))
    u.edits = edits
    return u


def ctx(args=None):
    return SimpleNamespace(user_data={}, args=args or [])


def gate(world, update, context):
    """Run the real ban_gate; returns True if it consumed the update."""
    try:
        run(bot.ban_gate(update, context))
        return False
    except ApplicationHandlerStop:
        return True


def button_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def button_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ---------------- needs_onboarding ----------------

def test_needs_onboarding_rules():
    assert bot.needs_onboarding({'sex': '👤', 'onboarding_done': False})
    assert bot.needs_onboarding({'sex': None, 'onboarding_done': None})
    assert bot.needs_onboarding({'sex': '', 'onboarding_done': False})
    assert not bot.needs_onboarding({'sex': '👨', 'onboarding_done': False})   # existing user who set sex
    assert not bot.needs_onboarding({'sex': '👩', 'onboarding_done': False})
    assert not bot.needs_onboarding({'sex': '👤', 'onboarding_done': True})    # hid sex AFTER onboarding
    assert not bot.needs_onboarding(None)


# ---------------- brand-new user ----------------

def test_new_user_start_asks_sex_first_and_holds_the_link(world):
    c = ctx(['comments_5'])
    run(bot.start(text_update(world, 77, '/start comments_5'), c))
    assert world.users['77']['sex'] == '👤'
    assert world.comments_shown == []                       # comments NOT shown yet
    text, markup = world.sent[-1]
    assert 'select your sex' in text.lower()
    assert button_data(markup) == ['onb_sex_male', 'onb_sex_female']   # no skip: sex is mandatory
    assert c.user_data['onboarding_payload'] == 'comments_5'


def test_full_flow_skip_nickname_then_comments_open(world):
    c = ctx(['comments_5'])
    run(bot.start(text_update(world, 77, '/start comments_5'), c))

    assert gate(world, tap_update(world, 77, 'onb_sex_male'), c)
    assert world.users['77']['sex'] == '👨' and world.users['77']['onboarding_done'] is True
    text, markup = world.sent[-1]
    assert 'nickname' in text.lower() and button_texts(markup) == ['Skip']
    assert bot.get_state(c) == bot.STATE_ONBOARDING_NICKNAME
    assert world.comments_shown == []                       # still waiting on the nickname answer

    assert gate(world, tap_update(world, 77, 'onb_skip_nick'), c)
    assert world.users['77']['anonymous_name'] == 'Anonymous'
    assert world.comments_shown == [5]                      # the link they clicked first now continues
    assert bot.get_state(c) == bot.STATE_IDLE
    assert 'onboarding_payload' not in c.user_data
    assert any(m == 'MAIN_MENU' for _, m in world.sent)     # reply keyboard installed for the new user


def test_full_flow_typed_nickname_then_comments_open(world):
    c = ctx(['comments_9'])
    run(bot.start(text_update(world, 78, '/start comments_9'), c))
    gate(world, tap_update(world, 78, 'onb_sex_female'), c)
    assert gate(world, text_update(world, 78, '  Grace  '), c)
    assert world.users['78']['sex'] == '👩'
    assert world.users['78']['anonymous_name'] == 'Grace'
    assert world.comments_shown == [9]


def test_no_deep_link_ends_with_normal_welcome(world):
    c = ctx([])
    run(bot.start(text_update(world, 79, '/start'), c))
    gate(world, tap_update(world, 79, 'onb_sex_male'), c)
    gate(world, tap_update(world, 79, 'onb_skip_nick'), c)
    assert world.comments_shown == []
    assert any('navigate' in t for t, _ in world.sent if isinstance(t, str))


def test_nickname_too_long_is_rejected_and_question_stays_open(world):
    c = ctx(['comments_5'])
    run(bot.start(text_update(world, 80, '/start comments_5'), c))
    gate(world, tap_update(world, 80, 'onb_sex_male'), c)
    assert gate(world, text_update(world, 80, 'x' * 31), c)
    assert world.users['80']['anonymous_name'] == 'Anonymous'
    assert bot.get_state(c) == bot.STATE_ONBOARDING_NICKNAME
    assert world.comments_shown == []


def test_menu_button_is_not_taken_as_a_nickname(world):
    c = ctx([])
    run(bot.start(text_update(world, 81, '/start'), c))
    gate(world, tap_update(world, 81, 'onb_sex_male'), c)
    assert gate(world, text_update(world, 81, 'Profile'), c) is False   # passes through to the normal handler
    assert world.users['81']['anonymous_name'] == 'Anonymous'
    assert bot.get_state(c) == bot.STATE_IDLE


# ---------------- existing users ----------------

def test_existing_user_with_sex_is_not_asked(world):
    world.add(5, sex='👩')
    c = ctx(['comments_7'])
    run(bot.start(text_update(world, 5, '/start comments_7'), c))
    assert world.comments_shown == [7]
    assert not any('select your sex' in str(t).lower() for t, _ in world.sent)
    assert gate(world, text_update(world, 5, 'Share'), c) is False


def test_existing_user_without_sex_is_asked_on_any_message(world):
    world.add(6, sex='👤')
    c = ctx()
    assert gate(world, text_update(world, 6, 'Share'), c)              # intercepted
    assert 'select your sex' in world.sent[-1][0].lower()
    assert gate(world, tap_update(world, 6, 'viewcomments_3_1'), c)    # old buttons too


def test_start_is_let_through_the_gate_so_start_can_hold_the_link(world):
    world.add(6, sex='👤')
    assert gate(world, text_update(world, 6, '/start comments_4'), ctx(['comments_4'])) is False
    assert gate(world, text_update(world, 6, '/start@testbot'), ctx()) is False


def test_admin_without_sex_is_never_locked_out(world):
    world.add(1, sex='👤', is_admin=True)
    assert gate(world, text_update(world, 1, '/admin'), ctx()) is False


def test_stale_sex_button_cannot_overwrite_sex(world):
    world.add(7, sex='👩', done=True)
    assert gate(world, tap_update(world, 7, 'onb_sex_male'), ctx())
    assert world.users['7']['sex'] == '👩'


def test_hiding_sex_later_does_not_bring_the_questions_back(world):
    c = ctx([])
    run(bot.start(text_update(world, 82, '/start'), c))
    gate(world, tap_update(world, 82, 'onb_sex_male'), c)
    gate(world, tap_update(world, 82, 'onb_skip_nick'), c)
    world.users['82']['sex'] = '👤'                                    # user picks "Remove/Hide Sex" in Profile
    assert gate(world, text_update(world, 82, 'Profile'), ctx()) is False


def test_skip_tap_outside_the_nickname_step_does_nothing(world):
    world.add(8, sex='👨', done=True)
    assert gate(world, tap_update(world, 8, 'onb_skip_nick'), ctx())
    assert world.users['8']['anonymous_name'] == 'Anonymous'


# ---------------- user who already chose a name but never set sex ----------------

def test_custom_name_without_sex_is_asked_sex_only_and_keeps_name(world):
    world.add(90, sex='👤', name='Grace')
    c = ctx(['comments_6'])
    run(bot.start(text_update(world, 90, '/start comments_6'), c))
    assert 'select your sex' in world.sent[-1][0].lower()

    assert gate(world, tap_update(world, 90, 'onb_sex_female'), c)
    assert world.users['90']['sex'] == '👩' and world.users['90']['onboarding_done'] is True
    assert world.users['90']['anonymous_name'] == 'Grace'            # untouched
    assert not any('nickname' in str(t).lower() and 'choose' in str(t).lower() for t, _ in world.sent)
    assert any('Your name stays Grace' in str(t) for t, _ in world.sent)
    assert bot.get_state(c) == bot.STATE_IDLE                         # not waiting for a nickname
    assert world.comments_shown == [6]                                # the link still continues


def test_custom_name_user_typing_after_sex_is_not_captured_as_nickname(world):
    world.add(91, sex='👤', name='Peter')
    c = ctx([])
    gate(world, text_update(world, 91, 'hello'), c)                   # intercepted, question shown
    gate(world, tap_update(world, 91, 'onb_sex_male'), c)
    assert gate(world, text_update(world, 91, 'Something else'), c) is False
    assert world.users['91']['anonymous_name'] == 'Peter'


def test_default_name_in_any_case_still_gets_the_nickname_question(world):
    world.add(92, sex='👤', name='anonymous')
    c = ctx([])
    gate(world, text_update(world, 92, 'hi'), c)
    gate(world, tap_update(world, 92, 'onb_sex_male'), c)
    assert bot.get_state(c) == bot.STATE_ONBOARDING_NICKNAME


# ---------------- only admins may hide their sex ----------------

def _edit_sex_buttons(world, uid):
    run(bot.button_handler(tap_update(world, uid, 'edit_sex'), ctx()))
    return button_data(world.sent[-1][1])


def test_non_admin_does_not_get_the_hide_sex_button(world):
    world.add(30, sex='👨', done=True)
    assert _edit_sex_buttons(world, 30) == ['sex_male', 'sex_female']


def test_admin_still_gets_the_hide_sex_button(world):
    world.add(31, sex='👨', done=True, is_admin=True)
    assert _edit_sex_buttons(world, 31) == ['sex_male', 'sex_female', 'sex_hide']


def test_non_admin_cannot_hide_sex_even_from_an_old_message(world):
    world.add(32, sex='👩', done=True)
    run(bot.button_handler(tap_update(world, 32, 'sex_hide'), ctx()))
    assert world.users['32']['sex'] == '👩'
    run(bot.button_handler(tap_update(world, 32, 'sex_whatever'), ctx()))   # unknown value must not reset it either
    assert world.users['32']['sex'] == '👩'


def test_admin_can_hide_and_non_admin_can_still_switch_male_female(world, monkeypatch):
    async def fake_profile(*a, **k):
        return None
    monkeypatch.setattr(bot, "send_updated_profile", fake_profile)
    world.add(33, sex='👨', done=True, is_admin=True)
    run(bot.button_handler(tap_update(world, 33, 'sex_hide'), ctx()))
    assert world.users['33']['sex'] == '👤'
    world.add(34, sex='👨', done=True)
    run(bot.button_handler(tap_update(world, 34, 'sex_female'), ctx()))
    assert world.users['34']['sex'] == '👩'
