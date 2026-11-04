from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.test_core import FakeModel
from whale_friend.backup import Backups
from whale_friend.config import Settings
from whale_friend.memory import MemoryEngine, tokens
from whale_friend.memory_actions import MemoryActions
from whale_friend.store import SCHEMA, Store


class MemoryHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=str(self.root / 'data'), backup_dir=str(self.root / 'cloud'))
        self.store = Store(Path(self.settings.data_dir))
        self.model = FakeModel()
        self.memory = MemoryEngine(self.store, self.model, self.settings)
        self.hub = self.memory.hub
        self.session = self.store.new_session()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, text):
        return self.store.add_message(self.session, 'user', text)

    def context(self, text):
        self.message(text)
        return self.memory.build_context(self.session, text, threading.Event())

    def card(self, identity):
        self.hub.index()
        return next(row for row in self.hub.topics() if row['id'] == identity)

    def test_legacy_migration_creates_snapshot_and_preserves_ids_and_sources(self):
        folder = self.root / 'legacy'
        folder.mkdir()
        db = sqlite3.connect(folder / 'companion.sqlite3')
        try:
            db.executescript(SCHEMA.split('CREATE TABLE IF NOT EXISTS memory_annotations')[0] + 'PRAGMA user_version=1;')
            db.execute("INSERT INTO sessions VALUES ('old','旧对话','2026-10-01')")
            db.execute("INSERT INTO messages VALUES (42,'old','user','我喜欢热拿铁','complete','2026-10-01','2026-10-01')")
            db.execute("INSERT INTO memories VALUES (77,'偏好','喜欢热拿铁','[42]','2026-10-01','2026-10-02')")
            db.commit()
        finally:
            db.close()
        upgraded = Store(folder)
        try:
            row = upgraded.memories()[0]
            self.assertEqual((row['id'], row['source_ids'], row['created_at']), (77, '[42]', '2026-10-01'))
            self.assertEqual(upgraded.messages('old')[0]['id'], 42)
            snapshot = list((folder / 'migrations').glob('before-memory-hub-*.sqlite3'))
            self.assertEqual(len(snapshot), 1)
            with closing(sqlite3.connect(snapshot[0])) as original:
                self.assertEqual(original.execute('SELECT id,content FROM memories').fetchone(), (77, '喜欢热拿铁'))
                self.assertFalse(original.execute("SELECT 1 FROM sqlite_master WHERE name='memory_topics'").fetchone())
        finally:
            upgraded.close()

    def test_pinned_and_stable_preferences_load_without_requesting_retrieval(self):
        pinned = self.store.add_memory('事实', '测试档案：正在准备海边旅行')
        preference = self.store.add_memory('相处偏好', '用户忙的时候少追问，先陪伴')
        self.hub.annotate(pinned, pinned=True)
        content = self.context('随便聊聊今天吧')[0]['content']
        self.assertIn('正在准备海边旅行', content)
        self.assertIn('少追问', content)
        self.assertEqual(set(self.hub.activation(self.session)['core_ids']), {pinned, preference})

    def test_work_topic_activates_implicitly_and_reads_originals_without_summary(self):
        identity = self.store.add_memory('偏好', '用户加班时希望先安静陪伴')
        packet = self.hub.activate(self.session, '今天又要熬夜赶进度')
        self.assertIn('work', packet['topic_ids'])
        content = self.context('今天又要熬夜赶进度')[0]['content']
        self.assertIn('加班时希望先安静陪伴', content)
        self.assertIn(identity, self.hub.activation(self.session)['memory_ids'])

    def test_empty_topics_and_assistant_catchphrases_do_not_crowd_activation(self):
        self.store.add_memory('事实', '用户加班时需要安静')
        self.store.add_memory('偏好', '用户喜欢拿铁')
        self.message('你好')
        self.store.add_message(self.session, 'assistant', '本鱼记得你喜欢拿铁。')
        packet = self.hub.activate(self.session, '要熬夜赶进度', track=False)
        self.assertEqual(packet['topic_ids'], ['work'])

    def test_semantic_topic_suggestions_work_when_no_alias_matches(self):
        identity = self.store.add_memory('事实', '小车摄像头预算最多300元')
        user = self.message('前面那个东西还得花多少钱？')
        with patch.object(self.model, 'complete', return_value=json.dumps({'action':'none','reason':'not_requested','topics':['car','does-not-exist']})):
            MemoryActions(self.memory).run(user, threading.Event())
        content = self.memory.build_context(self.session, '前面那个东西还得花多少钱？', threading.Event())[0]['content']
        self.assertIn('小车摄像头预算最多300元', content)
        self.assertIn(identity, self.hub.activation(self.session)['memory_ids'])
        self.assertEqual(self.hub.activation(self.session)['topic_ids'], ['car'])

    def test_auto_loading_still_works_with_memory_tools_disabled(self):
        self.settings.memory_tools_enabled = False
        identity = self.store.add_memory('事实', '小车底盘暂时选两轮结构')
        self.context('小车有进展吗')
        self.assertIn(identity, self.hub.activation(self.session)['memory_ids'])

    def test_proactive_reply_also_loads_and_tracks_core_and_topics(self):
        from whale_friend.presence import Presence
        identity = self.store.add_memory('事实', '用户加班时喜欢先安静陪伴')
        self.hub.annotate(identity, pinned=True)
        user_id = self.message('今晚又要熬夜赶进度')
        user = self.store.message_sources([user_id])[0]
        presence = Presence(self.store, self.model, self.settings)
        self.assertTrue(presence.compose(self.memory, user, threading.Event()))
        trace = self.hub.activation(self.session)
        self.assertIn(identity, trace['core_ids'])
        self.assertIn('work', trace['topic_ids'])
        self.assertIn('安静陪伴', self.model.calls[-1][1]['content'])

    def test_recent_focus_carries_short_followup_then_expires(self):
        self.store.add_memory('事实', '小车摄像头预算300元')
        self.context('小车')
        for _ in range(4):
            self.message('然后呢')
        self.assertIn('car', self.hub.activate(self.session, '然后呢', track=False)['topic_ids'])
        old = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
        self.store.execute('UPDATE memory_focus SET updated_at=?', (old,))
        self.assertEqual(self.hub.activate(self.session, '然后呢', track=False)['topic_ids'], [])

    def test_custom_topic_aliases_and_manual_assignment(self):
        identity = self.store.add_memory('目标', '准备英语考试')
        topic = self.hub.save_topic('考试计划', '英语，单词，背书')
        self.assertIn(topic, next(r for r in self.hub.overview()['records'] if r['id'] == identity)['topics'])
        self.hub.annotate(identity, topic_ids=['work'])
        self.assertEqual(next(r for r in self.hub.overview()['records'] if r['id'] == identity)['topics'], ['work'])
        self.hub.save_topic('考试计划', '英语，单词，背书，复习', topic)
        self.assertEqual(next(r for r in self.hub.overview()['records'] if r['id'] == identity)['topics'], ['work'])

    def test_role_play_does_not_become_user_food_preference(self):
        identity = self.store.add_memory('角色约定', '本鱼喜欢白米饭和咖啡，是玩笑约定')
        row = next(r for r in self.hub.overview()['records'] if r['id'] == identity)
        self.assertIn('character', row['topics'])
        self.assertNotIn('food', row['topics'])

    def test_idle_summary_is_grounded_incremental_and_keeps_originals(self):
        ids = [self.store.add_memory('事实', f'小车摄像头方案编号{i}') for i in range(15)]
        before = self.store.memories()
        self.assertGreater(self.hub.consolidate(force=True), 0)
        first = self.card('car')
        self.assertEqual(set(first['source_ids']), set(ids[:12]))
        self.assertTrue(first['dirty'])
        self.hub.consolidate(force=True)
        self.assertEqual(set(self.card('car')['source_ids']), set(ids))
        self.assertFalse(self.card('car')['dirty'])
        self.assertEqual(before, self.store.memories())

    def test_idle_wait_and_disabled_setting_avoid_model_calls(self):
        self.store.add_memory('事实', '小车正在规划')
        self.message('刚发来的话')
        self.assertEqual(self.hub.consolidate(), 0)
        self.settings.memory_consolidation_enabled = False
        self.assertEqual(self.hub.consolidate(), 0)
        self.assertEqual(self.model.topic_summaries, [])
        self.assertGreater(self.hub.consolidate(force=True), 0)

    def test_bad_summary_keeps_sources_and_automatic_recall(self):
        identity = self.store.add_memory('事实', '小车预算300元')
        with patch.object(self.model, 'complete', return_value='{"summary":"假的", "memory_ids":[99999]}'):
            self.assertEqual(self.hub.consolidate(force=True), 0)
        self.assertFalse(self.card('car')['summary'])
        self.assertTrue(self.store.meta('memory_hub_error'))
        self.assertIn('小车预算300元', self.context('小车还有多少预算')[0]['content'])
        self.assertEqual(self.store.memories()[0]['id'], identity)

    def test_edit_invalidates_old_summary_trace_and_preserves_revision(self):
        user = self.message('小车预算300元')
        identity = self.store.add_memory('事实', '小车预算300元', [user])
        self.hub.consolidate(force=True)
        self.context('小车')
        self.store.update_memory(identity, '事实', '小车预算500元')
        self.assertFalse(self.card('car')['summary'])
        self.assertIsNone(self.hub.activation(self.session))
        versions = self.store.rows('SELECT * FROM memory_versions WHERE memory_id=?', (identity,))
        self.assertEqual(versions[0]['content'], '小车预算300元')
        self.assertEqual(json.loads(versions[0]['source_ids']), [user])

    def test_stale_summary_response_after_source_edit_is_discarded(self):
        identity = self.store.add_memory('事实', '小车预算300元')
        def changed(messages, **kwargs):
            self.store.update_memory(identity, '事实', '小车预算500元')
            return json.dumps({'summary':'预算300元', 'memory_ids':[identity]})
        with patch.object(self.model, 'complete', side_effect=changed):
            self.assertEqual(self.hub.consolidate(force=True), 0)
        self.assertFalse(self.card('car')['summary'])

    def test_archiving_and_pinning_preserve_provenance_and_exclude_history(self):
        user = self.message('小车预算300元')
        identity = self.store.add_memory('事实', '小车预算300元', [user])
        before = self.store.memories()[0]
        self.hub.annotate(identity, pinned=True)
        self.assertEqual(before, self.store.memories()[0])
        self.hub.consolidate(force=True)
        self.hub.annotate(identity, active=False)
        self.assertFalse(self.card('car')['summary'])
        self.assertFalse(self.hub.core())
        self.assertNotIn(identity, [r['id'] for r in self.hub.activate(self.session, '小车')['records']])
        self.assertEqual(before, self.store.memories()[0])
        self.assertEqual(len(self.hub.overview()['records']), 1)

    def test_forgetting_removes_versions_annotations_and_card_sources(self):
        identity = self.store.add_memory('事实', '小车预算300元')
        self.hub.annotate(identity, pinned=True)
        self.store.update_memory(identity, '事实', '小车预算500元')
        self.hub.consolidate(force=True)
        self.store.forget_memory(identity)
        self.assertFalse(self.card('car')['summary'])
        self.assertFalse(self.store.rows('SELECT * FROM memory_versions'))
        self.assertFalse(self.store.rows('SELECT * FROM memory_annotations'))

    def test_backup_restores_core_topics_history_and_source_ids(self):
        identity = self.store.add_memory('事实', '小车预算300元')
        self.store.update_memory(identity, '事实', '小车预算500元')
        self.hub.annotate(identity, pinned=True)
        custom = self.hub.save_topic('测试车', '小车，摄像头')
        self.hub.consolidate(force=True)
        archive = Backups(self.store, self.settings).create()
        with zipfile.ZipFile(archive) as package:
            self.assertIn('exports/memory_versions.json', package.namelist())
        restored_dir = self.root / 'restored'
        Backups.restore(archive, restored_dir)
        restored = Store(restored_dir)
        try:
            hub = MemoryEngine(restored, self.model, self.settings).hub
            self.assertEqual(hub.core()[0]['id'], identity)
            self.assertTrue(any(row['id'] == custom for row in hub.topics()))
            self.assertEqual(restored.rows('SELECT content FROM memory_versions')[0]['content'], '小车预算300元')
        finally:
            restored.close()

    def test_core_limit_and_annotation_validation_do_not_change_records(self):
        for i in range(12):
            self.hub.annotate(self.store.add_memory('事实', f'固定档案{i}'), pinned=True)
        identity = self.store.add_memory('事实', '额外一条')
        with self.assertRaises(ValueError):
            self.hub.annotate(identity, pinned=True)
        with self.assertRaises(ValueError):
            self.hub.annotate(identity, topic_ids=['unknown'])
        with self.assertRaises(ValueError):
            self.hub.annotate(identity, pinned=True, active=False)
        self.assertEqual(len(self.store.memories()), 13)

    def test_budget_tracks_only_entries_actually_packed(self):
        ids = [self.store.add_memory('事实', f'稳定档案{i}：' + '测试事实' * 100) for i in range(12)]
        for identity in ids:
            self.hub.annotate(identity, pinned=True)
        ids += [self.store.add_memory('相处偏好', f'相处约定{i}：' + '温柔陪伴' * 30) for i in range(6)]
        self.settings.context_tokens = 4096
        self.settings.reply_tokens = 256
        packed = self.context('你好')
        actual = self.hub.activation(self.session)
        self.assertEqual(set(actual['core_ids']), set(ids))
        for identity in actual['memory_ids']:
            self.assertIn(f'记忆#{identity} ', packed[0]['content'])
        self.assertLessEqual(sum(tokens(m['content']) + 16 for m in packed), 4096 - 256 - 512)

    def test_large_topic_batches_preserve_valid_json_with_small_context(self):
        self.settings.context_tokens = 4096
        self.settings.reply_tokens = 512
        for i in range(20):
            self.store.add_memory('事实', f'小车事项{i}：' + '摄像头方案测试' * 45)
        self.assertGreater(self.hub.consolidate(force=True), 0)
        self.assertTrue(self.card('car')['source_ids'])
        self.assertTrue(self.card('car')['dirty'])
        data = json.loads(self.model.topic_summaries[0][1]['content'])
        self.assertLess(len(data['new_memories']), 12)


if __name__ == '__main__':
    unittest.main()
