import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from tests.test_core import FakeModel
from whale_friend.backup import Backups
from whale_friend.config import Settings
from whale_friend.memory import MemoryEngine,tokens
from whale_friend.memory_actions import MemoryActions
from whale_friend.memory_signals import fingerprint
from whale_friend.store import SCHEMA,Store


class SignalModel(FakeModel):
    def __init__(self,recall_id=None):
        super().__init__()
        self.recall_id=recall_id

    def complete(self,messages,**kwargs):
        self.calls.append(messages)
        if '你是主题记忆归纳器' in messages[0]['content']:
            data=json.loads(messages[1]['content'])
            ids=[row['id'] for row in data['new_memories']]
            return json.dumps({'summary':'历史上曾因岗位变化感到压力，需结合当前情况核对。','memory_ids':ids,
                              'recall_cues':['好累','压力','疲惫'],'annotations':[{'id':key,'importance':2,'cues':['好累','疲惫']} for key in ids]},ensure_ascii=False)
        return json.dumps({'action':'none','reason':'not_requested','topics':['work'],'recall_ids':[self.recall_id] if self.recall_id else [],'recall_reason':'疲惫→以前明确记录的工作压力'},ensure_ascii=False)


class MemorySignalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.settings=Settings(data_dir=str(self.root/'data'),adaptive_enabled=False)
        self.store=Store(self.root/'data')
        self.model=SignalModel()
        self.memory=MemoryEngine(self.store,self.model,self.settings)
        self.hub=self.memory.hub
        self.session=self.store.new_session()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def fact(self,text,kind='事实',raw=None):
        source=self.store.add_message(self.session,'user',raw or text)
        key=self.store.add_memory(kind,text,[source])
        return key,source

    def pair(self,raw='测试小车摄像头已经换成CSI接口了'):
        old,source=self.fact('测试小车摄像头使用USB接口')
        new,evidence=self.fact('测试小车摄像头使用CSI接口',raw=raw)
        rows={row['id']:row for row in self.store.memories()}
        proposal={'previous_id':old,'newer_id':new,'evidence_ids':[evidence],'subject':'测试小车','anchor':'摄像头',
                  'previous_value':'USB接口','newer_value':'CSI接口','explanation':'摄像头接口明确变更'}
        return old,new,proposal,rows

    def test_semantic_planner_gets_experiences_and_loads_without_keyword_overlap(self):
        identity,source=self.fact('用户曾因担心岗位被自动化替代而承受工作压力')
        self.model.recall_id=identity
        current=self.store.add_message(self.session,'user','今天好累')
        result=MemoryActions(self.memory).run(current,threading.Event())
        self.assertEqual(result['status'],'skipped')
        self.assertIn('岗位被自动化替代',self.model.calls[-1][1]['content'])
        context=self.memory.build_context(self.session,'今天好累',threading.Event())
        self.assertIn('岗位被自动化替代',context[0]['content'])
        trace=self.hub.activation(self.session)
        self.assertIn(identity,trace['memory_ids'])
        self.assertIn('以前明确记录',trace['recall_reason'])
        self.assertIn('不是当前原因',context[0]['content'])
        self.assertEqual(len(self.store.memories()),1)

    def test_learned_cues_work_even_when_conversational_tools_disabled(self):
        identity,source=self.fact('用户曾为岗位被替代的可能性而担忧，涉及工作')
        self.hub.consolidate(force=True)
        self.settings.memory_tools_enabled=False
        self.store.add_message(self.session,'user','今天好累')
        packet=self.hub.activate(self.session,'今天好累')
        self.assertIn(identity,[row['id'] for row in packet['records']])
        self.assertIn('work',packet['topic_ids'])

    def test_important_experience_outranks_many_jokes_and_manual_priority_survives_model(self):
        important,source=self.fact('用户不希望被反复追问恋爱话题，这是长期边界','偏好')
        self.hub.signals.set_importance(important,3)
        for i in range(14): self.fact(f'角色约定：本鱼吃白饭的第{i}个玩笑','角色约定')
        self.hub.index()
        self.assertIn(important,[row['id'] for row in self.hub.core()])
        row=self.store.rows('SELECT * FROM memories WHERE id=?',(important,))[0]
        topic=next(t for t in self.hub.topics() if t['id']=='other')
        with self.store.lock,self.store.db:
            self.hub.signals.apply_analysis(topic,{'annotations':[{'id':important,'importance':0,'cues':['边界']} ]},[row])
        saved=next(row for row in self.hub.records() if row['id']==important)
        self.assertEqual(saved['importance'],3)
        self.assertTrue(saved['importance_manual'])

    def test_explicit_state_update_archives_old_preserves_sources_and_is_reversible(self):
        old,new,proposal,rows=self.pair()
        self.hub.annotate(old,pinned=True)
        original=self.store.memories()
        current=self.store.add_message(self.session,'user','旧接口是什么')
        self.hub.suggest(self.session,current,['car'],[old],'旧摄像头接口')
        self.memory.build_context(self.session,'旧接口是什么',threading.Event())
        self.assertTrue(self.store.rows('SELECT * FROM memory_recall_traces'))
        self.store.set_summary(self.session,'摄像头还是USB',1)
        with self.store.lock,self.store.db:
            identity=self.hub.signals.propose_change(proposal,rows)
        self.assertEqual(self.hub.signals.changes()[0]['status'],'applied')
        records={r['id']:r for r in self.hub.records(include_inactive=True)}
        self.assertFalse(records[old]['active'])
        self.assertTrue(records[new]['pinned'])
        self.assertEqual(self.store.memories(),original)
        self.assertEqual(self.store.summary(self.session)['content'],'')
        self.assertEqual(self.store.rows('SELECT * FROM memory_recall_traces'),[])
        self.hub.signals.resolve(identity,'undo')
        records={r['id']:r for r in self.hub.records(include_inactive=True)}
        self.assertTrue(records[old]['active'])
        self.assertFalse(records[new]['active'])
        self.assertTrue(records[old]['pinned'])

    def test_uncertain_plans_stay_pending_and_can_be_reviewed(self):
        old,new,proposal,rows=self.pair('如果测试小车摄像头换成CSI接口，可能更好，我还在考虑')
        with self.store.lock,self.store.db:
            identity=self.hub.signals.propose_change(proposal,rows)
        self.assertEqual(self.hub.signals.changes()[0]['status'],'pending')
        self.assertTrue(all(row['active'] for row in self.hub.records()))
        self.store.add_message(self.session,'user','摄像头现在是什么接口')
        context=self.memory.build_context(self.session,'摄像头',threading.Event())[0]['content']
        self.assertIn('状态待核对',context)
        self.hub.signals.resolve(identity,'reject')
        self.assertEqual(self.hub.signals.changes()[0]['status'],'rejected')
        with self.store.lock,self.store.db:
            self.hub.signals.propose_change(proposal,rows)
        self.assertEqual(self.hub.signals.changes()[0]['status'],'rejected')

    def test_disable_auto_update_keeps_review_and_stale_edits_cannot_be_applied(self):
        self.settings.memory_evolution_enabled=False
        old,new,proposal,rows=self.pair()
        with self.store.lock,self.store.db:
            identity=self.hub.signals.propose_change(proposal,rows)
        self.assertEqual(self.hub.signals.changes()[0]['status'],'pending')
        self.store.update_memory(new,'事实','测试小车摄像头使用另一种接口')
        with self.assertRaisesRegex(ValueError,'已经修改'):
            self.hub.signals.resolve(identity,'accept')
        self.assertTrue(next(r for r in self.hub.records() if r['id']==old)['active'])

    def test_wrong_entity_or_unoffered_ids_cannot_auto_replace(self):
        old,new,proposal,rows=self.pair()
        proposal['subject']='另一辆小车'
        with self.store.lock,self.store.db:
            identity=self.hub.signals.propose_change(proposal,rows)
        self.assertEqual(self.hub.signals.changes()[0]['status'],'pending')
        proposal['newer_id']=99999
        with self.store.lock,self.store.db:
            self.assertIsNone(self.hub.signals.propose_change(proposal,rows))

    def test_assistant_or_forgotten_evidence_cannot_propose_state_changes(self):
        old,new,proposal,rows=self.pair()
        evidence=proposal['evidence_ids'][0]
        self.store.execute("UPDATE messages SET role='assistant' WHERE id=?",(evidence,))
        with self.store.lock,self.store.db:
            self.assertIsNone(self.hub.signals.propose_change(proposal,rows))
        self.store.execute("UPDATE messages SET role='user' WHERE id=?",(evidence,))
        self.store.execute('INSERT INTO forgotten_sources VALUES(?)',(evidence,))
        with self.store.lock,self.store.db:
            self.assertIsNone(self.hub.signals.propose_change(proposal,rows))

    def test_derived_cues_disappear_after_source_edit_and_directory_json_stays_valid(self):
        identity,source=self.fact('用户曾为工作变动而担忧')
        self.hub.consolidate(force=True)
        self.assertTrue(next(r for r in self.hub.records() if r['id']==identity)['recall_cues'])
        self.store.update_memory(identity,'事实','现在只保留另一件事实')
        self.assertEqual(next(r for r in self.hub.records() if r['id']==identity)['recall_cues'],[])
        text,offered=self.hub.directory_prompt(180)
        self.assertIsInstance(json.loads(text),list)
        self.assertLessEqual(tokens(text),180)

    def test_backup_preserves_priorities_changes_and_original_history(self):
        old,new,proposal,rows=self.pair()
        self.hub.signals.set_importance(old,2)
        with self.store.lock,self.store.db: self.hub.signals.propose_change(proposal,rows)
        archive=Backups(self.store,self.settings).create(self.root/'cloud')
        destination=self.root/'restored'
        Backups.restore(archive,destination)
        restored=Store(destination)
        try:
            self.assertEqual(restored.rows('SELECT status FROM memory_changes')[0]['status'],'applied')
            self.assertEqual(len(restored.memories()),2)
            self.assertEqual(restored.rows('SELECT importance FROM memory_signals WHERE memory_id=?',(new,))[0]['importance'],2)
        finally: restored.close()

    def test_upgrade_from_previous_layered_memory_makes_online_snapshot(self):
        folder=self.root/'previous'
        folder.mkdir()
        prior=SCHEMA[:SCHEMA.index('CREATE TABLE IF NOT EXISTS memory_signals')]+SCHEMA[SCHEMA.index('CREATE TRIGGER IF NOT EXISTS memory_hub_insert'):]
        with closing(sqlite3.connect(folder/'companion.sqlite3')) as db:
            db.executescript(prior)
            db.execute("INSERT INTO memories VALUES(77,'事实','旧记忆不能丢','[]','2026-01-01','2026-01-01')")
            db.commit()
        upgraded=Store(folder)
        try:
            self.assertEqual(upgraded.memories()[0]['id'],77)
            self.assertEqual(len(list((folder/'migrations').glob('before-memory-signals-*.sqlite3'))),1)
        finally: upgraded.close()

    def test_different_project_qualifiers_prevent_automatic_replacement(self):
        old,source=self.fact('蓝鲸小车摄像头使用USB接口')
        new,evidence=self.fact('蓝鲨小车摄像头使用CSI接口',raw='蓝鲨小车摄像头已经换成CSI接口了')
        rows={row['id']:row for row in self.store.memories()}
        proposal={'previous_id':old,'newer_id':new,'evidence_ids':[evidence],'subject':'小车','anchor':'摄像头',
                  'previous_value':'USB接口','newer_value':'CSI接口'}
        with self.store.lock,self.store.db: self.hub.signals.propose_change(proposal,rows)
        self.assertEqual(self.hub.signals.changes()[0]['status'],'pending')

    def test_superseded_source_cannot_be_reextracted_but_fresh_return_state_gets_new_id(self):
        old,new,proposal,rows=self.pair()
        with self.store.lock,self.store.db: self.hub.signals.propose_change(proposal,rows)
        old_sources=json.loads(rows[old]['source_ids'])
        self.assertIsNone(self.store.add_memory('事实','改写措辞：测试小车摄像头仍是USB接口',old_sources))
        source=self.store.add_message(self.session,'user','测试小车摄像头现在又换回USB接口了')
        returned=self.store.add_memory('事实',rows[old]['content'],[source])
        self.assertIsNotNone(returned)
        self.assertNotEqual(returned,old)
        backward={'previous_id':new,'newer_id':returned,'evidence_ids':[source],'subject':'测试小车','anchor':'摄像头',
                  'previous_value':'CSI接口','newer_value':'USB接口'}
        records={r['id']:r for r in self.store.memories()}
        with self.store.lock,self.store.db: self.hub.signals.propose_change(backward,records)
        active={r['id'] for r in self.hub.records()}
        self.assertIn(returned,active)
        self.assertNotIn(new,active)
        self.assertEqual(len(self.store.memories()),3)

    def test_deleting_memory_clears_derived_recall_reason_and_cues(self):
        identity,source=self.fact('用户曾为工作变动而担忧')
        self.hub.consolidate(force=True)
        current=self.store.add_message(self.session,'user','今天好累')
        self.hub.suggest(self.session,current,['work'],[identity],'曾经的工作担忧')
        self.memory.build_context(self.session,'今天好累',threading.Event())
        self.assertTrue(self.store.rows('SELECT * FROM memory_recall_traces'))
        self.store.forget_memory(identity)
        self.assertEqual(self.store.rows('SELECT * FROM memory_recall_traces'),[])
        self.assertEqual(self.store.rows('SELECT * FROM memory_topic_cues'),[])
        packet=self.hub.activate(self.session,'今天好累')
        self.assertEqual(packet['recall_reason'],'')
        self.assertEqual(packet['records'],[])

    def test_multiple_change_clauses_require_review_instead_of_guessing_latest_value(self):
        old,new,proposal,rows=self.pair('测试小车摄像头先换成CSI接口，后来又换回USB接口')
        with self.store.lock,self.store.db: self.hub.signals.propose_change(proposal,rows)
        self.assertEqual(self.hub.signals.changes()[0]['status'],'pending')


if __name__=='__main__': unittest.main()
