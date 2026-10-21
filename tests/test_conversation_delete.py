from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from src.core.deps import ActorScope
from src.core.exceptions import NotFound
from src.services.chat import delete_conversation


async def test_delete_locks_only_owned_conversation_and_removes_messages():
    actor = ActorScope('buyer', 'demo', 'buyer', ['shop-a'])
    record = SimpleNamespace(id=uuid4())
    db = MagicMock()
    db.scalar = AsyncMock(return_value=record)
    db.execute = AsyncMock()
    db.delete = AsyncMock()
    await delete_conversation(db, actor, record.id, 'trace')
    lookup = db.scalar.call_args.args[0].compile(dialect=postgresql.dialect())
    assert 'FOR UPDATE' in str(lookup)
    assert 'demo' in lookup.params.values() and 'buyer' in lookup.params.values()
    deletion = db.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert record.id in deletion.params.values() and 'demo' in deletion.params.values()
    db.delete.assert_awaited_once_with(record)
    assert db.add.call_args.args[0].action == 'conversation.deleted'


async def test_unknown_or_unowned_conversation_cannot_be_deleted():
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.execute = AsyncMock()
    db.delete = AsyncMock()
    with pytest.raises(NotFound):
        await delete_conversation(db, ActorScope('buyer', 'demo', 'buyer', ['shop-a']), uuid4(), 'trace')
    db.execute.assert_not_awaited()
    db.delete.assert_not_awaited()
