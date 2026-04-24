import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_mod


class _PostsResult:
    def __init__(self, posts):
        self._posts = posts

    def scalars(self):
        return self

    def all(self):
        return self._posts


class _ClaimResult:
    def __init__(self, calls, post_id=None):
        self._calls = calls
        self._post_id = post_id

    def scalar_one_or_none(self):
        self._calls.append("scalar")
        return self._post_id


class _SessionStub:
    def __init__(self, calls, posts=None, post_id=None):
        self._calls = calls
        self._posts = posts if posts is not None else [SimpleNamespace(id=1)]
        self._post_id = post_id  # None → claim returns None (skip); non-None → claimed
        self._execute_count = 0
        self.committed = []

    async def execute(self, _stmt):
        if self._execute_count == 0:
            self._execute_count += 1
            self._calls.append("select_due_posts")
            return _PostsResult(self._posts)
        self._calls.append("claim_post")
        return _ClaimResult(self._calls, self._post_id)

    async def get(self, model, pk):
        return None

    async def commit(self):
        self._calls.append("commit")
        self.committed.append(True)


class _SessionMakerStub:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_claim_row_is_read_before_commit(mocker):
    calls = []
    session = _SessionStub(calls)
    mocker.patch.object(main_mod, "async_session_maker", _SessionMakerStub(session))

    await main_mod._do_publish_scheduled_posts(AsyncMock())

    assert "scalar" in calls
    assert "commit" in calls
    assert calls.index("scalar") < calls.index("commit")


@pytest.mark.asyncio
async def test_empty_post_sets_error_without_calling_telegram(mocker):
    """Пост без текста и без медиафайла должен получить статус 'error'
    без попытки вызвать Telegram Bot API."""
    calls = []
    # Пост claimed успешно (id=42), но content=None, file_id=None
    post = SimpleNamespace(
        id=42,
        status="pending",
        content=None,
        file_id=None,
        file_type=None,
        signature=None,
        inline_buttons=None,
        channel_id=1,
        created_by=None,
        crosspost_to_max=False,
    )
    session = _SessionStub(calls, posts=[post], post_id=42)

    fake_channel = SimpleNamespace(
        id=1,
        name="Test Channel",
        telegram_id=-1001234567890,
        username=None,
    )
    session.get = AsyncMock(return_value=fake_channel)

    mocker.patch.object(main_mod, "async_session_maker", _SessionMakerStub(session))
    mocker.patch.object(main_mod, "ADMIN_IDS", [100])

    bot = AsyncMock()
    await main_mod._do_publish_scheduled_posts(bot)

    # send_photo / send_video / send_document must NOT have been called
    bot.send_photo.assert_not_called()
    bot.send_video.assert_not_called()
    bot.send_document.assert_not_called()

    # send_message must have been called only for the admin notification,
    # NOT for sending the actual post content to the channel.
    assert bot.send_message.call_count == 1
    notify_args = bot.send_message.call_args[0]
    # First positional arg is the recipient – must be the admin, not the channel
    assert notify_args[0] == 100
    assert "не содержит" in notify_args[1]

    # Post status should be set to "error"
    assert post.status == "error"


@pytest.mark.asyncio
async def test_send_error_notification_includes_error_hint(mocker):
    """При ошибке Telegram API уведомление администратора должно содержать
    строку с описанием причины (error_hint)."""
    calls = []
    post = SimpleNamespace(
        id=7,
        status="pending",
        content="Some content",
        file_id=None,
        file_type=None,
        signature=None,
        inline_buttons=None,
        channel_id=1,
        created_by=None,
        crosspost_to_max=False,
    )
    session = _SessionStub(calls, posts=[post], post_id=7)

    fake_channel = SimpleNamespace(
        id=1,
        name="My Channel",
        telegram_id=-1009999999999,
        username=None,
    )
    session.get = AsyncMock(return_value=fake_channel)

    mocker.patch.object(main_mod, "async_session_maker", _SessionMakerStub(session))
    mocker.patch.object(main_mod, "ADMIN_IDS", [999])

    # Telegram API fails with a known-looking error
    api_error = Exception("Forbidden: bot is not a member of the channel chat")

    bot = AsyncMock()
    bot.send_message.side_effect = [
        api_error,   # first call = sending the actual post → raises
        None,        # second call = admin notification → succeeds
    ]

    await main_mod._do_publish_scheduled_posts(bot)

    # Two send_message calls expected: one for the post, one for the admin
    assert bot.send_message.call_count == 2

    # The admin notification (second call) must contain a non-empty "Причина:" line
    admin_notify_text = bot.send_message.call_args_list[1][0][1]
    assert "Причина:" in admin_notify_text
    assert len(admin_notify_text.split("Причина:")[1].strip()) > 0

    # Post status must be "error"
    assert post.status == "error"
