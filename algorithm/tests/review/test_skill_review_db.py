from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
)

from lazymind.review.skill_review import db as skill_review_db


def _read_eligible_sessions():
    engine = create_engine('sqlite+pysqlite:///:memory:', future=True)
    metadata = MetaData()
    conversations = Table(
        'conversations',
        metadata,
        Column('id', String, primary_key=True),
        Column('create_user_id', String, nullable=False),
        Column('updated_at', DateTime(timezone=True), nullable=False),
    )
    chat_histories = Table(
        'chat_histories',
        metadata,
        Column('id', String, primary_key=True),
        Column('conversation_id', String, nullable=False),
        Column('seq', Integer, nullable=False),
        Column('content', Text),
        Column('result', Text),
        Column('create_time', DateTime(timezone=True), nullable=False),
    )
    plugin_sessions = Table(
        'plugin_sessions',
        metadata,
        Column('id', String, primary_key=True),
        Column('conversation_id', String, nullable=False),
        Column('status', String, nullable=False),
        Column('dismissed', Integer, nullable=False),
    )
    metadata.create_all(engine)

    start = datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(conversations.insert(), [
            {'id': 'conv-regular', 'create_user_id': 'user-1', 'updated_at': start + timedelta(minutes=10)},
            {'id': 'conv-plugin', 'create_user_id': 'user-1', 'updated_at': start + timedelta(minutes=20)},
        ])
        conn.execute(chat_histories.insert(), [
            {
                'id': 'history-regular',
                'conversation_id': 'conv-regular',
                'seq': 1,
                'content': 'regular request',
                'result': 'regular response',
                'create_time': start + timedelta(minutes=10),
            },
            {
                'id': 'history-plugin',
                'conversation_id': 'conv-plugin',
                'seq': 1,
                'content': 'plugin request',
                'result': 'plugin response',
                'create_time': start + timedelta(minutes=20),
            },
        ])
        conn.execute(plugin_sessions.insert(), {
            'id': 'plugin-session-1',
            'conversation_id': 'conv-plugin',
            'status': 'completed',
            'dismissed': 1,
        })

    with patch.object(skill_review_db, '_get_app_conn', return_value=engine):
        return skill_review_db.read_session(start, start + timedelta(hours=1))


class TestSkillReviewDB(unittest.TestCase):
    def test_read_session_excludes_conversations_with_plugin_sessions(self):
        sessions = _read_eligible_sessions()

        self.assertEqual(
            [session['conversation_id'] for session in sessions],
            ['conv-regular'],
        )
