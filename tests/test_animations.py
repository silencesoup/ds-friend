import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.test_core import FakeModel
from whale_friend.animations import CATALOG, IDLE, AnimationDirector, MotionFilter
from whale_friend.avatar_ui import ClipDecoder, video_frames
from whale_friend.backup import Backups
from whale_friend.config import Settings
from whale_friend.memory import tokens
from whale_friend.service import Companion
from whale_friend.store import Store


class MotionModel(FakeModel):
    def __init__(self,name='吃白饭'):
        super().__init__()
        self.name = name

    def stream(self,messages,cancel,**kwargs):
        self.calls.append(messages)
        yield 'Deep！Deep！本鱼会陪你上班呀。[[mo'
        yield 'tion:'+self.name+']]'


class AnimationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=str(self.root/'data'),memory_tools_enabled=False,proactive_enabled=False,adaptive_enabled=False)
        self.core = Companion(self.settings,model=MotionModel())
        self.core.maintain = lambda **kwargs:False
        self.session = self.core.store.new_session()

    def tearDown(self):
        self.core.shutdown()
        self.temp.cleanup()

    def reply(self):
        self.core.send(self.session,'小肥鱼，别只吃白饭啦，陪我上班吧')
        deadline = time.monotonic()+5
        while self.core.chat_pending and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertFalse(self.core.chat_pending)
        return self.core.store.messages(self.session)[-1]

    def test_every_vendored_clip_has_editable_description_and_local_file(self):
        catalog = self.core.animations.catalog()
        self.assertEqual(len(catalog),109)
        for clip in catalog:
            self.assertTrue(clip['description'])
            self.assertTrue(self.core.animations.path(clip['id']).is_file())
            self.assertNotIn('path',clip)

    def test_reply_selects_action_without_leaking_marker_or_second_model_call(self):
        self.core.store.add_memory('相处偏好','忙时安静陪伴')
        reply = self.reply()
        self.assertEqual(reply['status'],'complete')
        self.assertNotIn('motion',reply['content'])
        selected = self.core.store.rows('SELECT * FROM message_animations WHERE message_id=?',(reply['id'],))[0]
        self.assertEqual((selected['clip_id'],selected['source']),('吃白饭','model'))
        self.assertEqual(len(self.core.model.calls),1)
        self.assertIn('忙时安静陪伴',self.core.model.calls[0][0]['content'])
        snapshot = self.core.animations.snapshot(self.session)
        self.assertEqual(snapshot['id'],'吃白饭')
        self.assertEqual(snapshot['event'],'message:'+str(reply['id']))

    def test_reserved_or_unknown_model_choices_fall_back_without_false_state(self):
        self.core.model.name='余额-分文不剩'
        reply = self.reply()
        selected = self.core.store.rows('SELECT * FROM message_animations WHERE message_id=?',(reply['id'],))[0]
        self.assertEqual((selected['clip_id'],selected['source']),(IDLE,'mood'))
        self.assertNotIn('motion',reply['content'])
        with self.assertRaises(ValueError):
            self.core.animations.path('../../.env')

    def test_prompt_is_bounded_and_automatically_finds_topic_clips(self):
        prompt,offered = self.core.animations.prompt('能拉一段小提琴吗，顺便玩魔方',budget=600)
        self.assertLessEqual(tokens(prompt),600)
        self.assertIn('小提琴演奏',offered)
        self.assertIn('原地专心玩魔方',offered)
        self.assertNotIn('余额-分文不剩',offered)
        self.assertNotIn('睡觉-开始',offered)

    def test_old_reply_is_not_replayed_and_sessions_do_not_cross(self):
        reply = self.reply()
        self.core.store.execute('UPDATE message_animations SET selected_at=?',(time.time()-120,))
        self.assertTrue(self.core.animations.snapshot(self.session)['event'].startswith('mood:'))
        self.assertTrue(self.core.animations.snapshot(self.core.store.new_session())['event'].startswith('mood:'))

    def test_disable_animation_preserves_text_and_memory(self):
        self.settings.animation_enabled=False
        reply = self.reply()
        self.assertEqual(reply['status'],'complete')
        self.assertEqual(self.core.animations.snapshot(self.session),{'enabled':False})
        self.assertEqual(self.core.store.rows('SELECT * FROM message_animations'),[])

    def test_description_edit_is_immediate_and_separate_from_upstream(self):
        copy = self.root/'descriptions.json'
        copy.write_bytes(CATALOG.read_bytes())
        director = AnimationDirector(self.core,catalog_path=copy)
        director.update_description('吃白饭','捧着米饭，和用户开吃白饭的玩笑',['白饭专用词'],['开心'])
        prompt,offered = director.prompt('白饭专用词',budget=450)
        self.assertIn('捧着米饭',prompt)
        self.assertIn('吃白饭',offered)
        self.assertNotEqual(copy.read_bytes(),CATALOG.read_bytes())

    def test_backup_round_trip_keeps_selection_and_original_memories(self):
        self.core.store.add_memory('事实','原始记忆还在')
        reply = self.reply()
        archive = Backups(self.core.store,self.settings).create(self.root/'backups')
        destination = self.root/'recovered'
        Backups.restore(archive,destination)
        store = Store(destination)
        try:
            self.assertEqual(store.rows('SELECT clip_id FROM message_animations WHERE message_id=?',(reply['id'],))[0]['clip_id'],'吃白饭')
            self.assertTrue(any(row['content']=='原始记忆还在' for row in store.memories()))
        finally:
            store.close()

    def test_video_decoder_preserves_transparency_and_bounds_memory(self):
        path = self.core.animations.path(IDLE)
        frames = video_frames(path,500,200)
        image,delay = next(frames)
        self.assertEqual(image.mode,'RGBA')
        self.assertEqual(image.getchannel('A').getextrema(),(0,255))
        self.assertEqual(image.getchannel('A').crop((0,0,1,image.height)).getextrema(),(0,0))
        self.assertLessEqual(image.height,200)
        self.assertTrue(10<delay<100)
        frames.close()
        decoder = ClipDecoder()
        decoder.play(path,500,200)
        try:
            epoch,frame,delay,kind = decoder.frames.get(timeout=3)
            self.assertIsNotNone(frame)
            self.assertLessEqual(decoder.frames.qsize(),3)
        finally:
            decoder.close()
            decoder.thread.join(2)
        self.assertFalse(decoder.thread.is_alive())

    def test_long_persona_keeps_animation_protocol_and_topical_candidate(self):
        self.core.memory.persona=lambda:'角色设定很长。'*1200
        self.core.store.add_message(self.session,'user','请表演小提琴')
        prompt,offered=self.core.animations.prompt('请表演小提琴',budget=850)
        context=self.core.memory.build_context(self.session,'请表演小提琴',threading.Event(),animation_context=prompt)
        self.assertIn('[[motion:',context[0]['content'])
        self.assertIn('小提琴演奏：',context[0]['content'])
        self.assertLessEqual(sum(tokens(m['content'])+16 for m in context),self.settings.context_tokens-self.settings.reply_tokens-512)

    def test_removed_dsh_path_falls_back_to_bundled_main_avatar(self):
        self.settings.pet_plugin_dir=str(self.root/'uninstalled-dsh')
        self.core.animations.reload()
        self.assertEqual(len(self.core.animations.catalog()),109)
        self.assertTrue(self.core.animations.path(IDLE).is_file())


class MotionFilterTests(unittest.TestCase):
    def test_all_marker_split_positions_are_hidden_and_selection_is_whitelisted(self):
        raw='你好[[motion:吃白饭]]！'
        for position in range(len(raw)+1):
            selector=MotionFilter(['吃白饭'])
            answer=selector.feed(raw[:position])+selector.feed(raw[position:])+selector.feed('',final=True)
            self.assertEqual(answer,'你好！')
            self.assertEqual(selector.selected,'吃白饭')

    def test_invalid_multiple_and_truncated_markers_never_leak(self):
        selector=MotionFilter(['吃白饭'])
        self.assertEqual(selector.feed('hi[[motion:../../.env]][[motion:吃白饭]][[motion:东张西望]]',final=True),'hi')
        self.assertEqual(selector.selected,'吃白饭')
        self.assertEqual(MotionFilter([]).feed('你好[[motion:还没结束',final=True),'你好')
        selector=MotionFilter([])
        self.assertEqual(selector.feed('ok[[motion:'+'x'*1000),'ok')
        self.assertEqual(selector.feed('x]]继续',final=True),'继续')


if __name__=='__main__':
    unittest.main()
