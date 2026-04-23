import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    def __init__(self, calls):
        self._calls = calls

    def scalar_one_or_none(self):
        self._calls.append("scalar")
        return None


class _SessionStub:
    def __init__(self, calls):
        self._calls = calls
        self._execute_count = 0

    async def execute(self, _stmt):
        if self._execute_count == 0:
            self._execute_count += 1
            self._calls.append("select_due_posts")
            return _PostsResult([SimpleNamespace(id=1)])
        self._calls.append("claim_post")
        return _ClaimResult(self._calls)

    async def commit(self):
        self._calls.append("commit")


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
