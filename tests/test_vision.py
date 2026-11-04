from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests.test_core import FakeModel
from whale_friend.assets import AssetLibrary
from whale_friend.config import Settings
from whale_friend.memory import MemoryEngine, tokens
from whale_friend.model import Cancelled, ModelClient, ModelError
from whale_friend.service import Companion
from whale_friend.store import Store


class VisionModel(FakeModel):
    vision_available = True

    def ensure_vision(self, cancel=None, force=False):
        return self.vision_available


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=str(self.root / 'data'), proactive_enabled=False, adaptive_enabled=False)
        self.settings.vision_mode = 'on'
        self.store = Store(Path(self.settings.data_dir))
        self.assets = AssetLibrary(self.store)
        self.model = VisionModel()
        self.memory = MemoryEngine(self.store, self.model, self.settings)
        self.session = self.store.new_session()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def picture(self, color='red', size=(80, 60), animated=False):
        buffer = io.BytesIO()
        image = Image.new('RGB', size, color)
        image.save(buffer, format='GIF' if animated else 'PNG', **({'save_all':True, 'append_images':[Image.new('RGB', size, 'blue')], 'duration':100} if animated else {}))
        return self.assets.import_bytes(buffer.getvalue(), original_name='test.gif' if animated else 'test.png')

    def build(self, text='请描述这张图片'):
        return self.memory.build_context(self.session, text, threading.Event())

    def test_attached_image_reaches_model_as_real_content_blocks(self):
        asset = self.picture()
        source = self.store.add_message(self.session, 'user', '这张图有什么？', attachments=[asset['id']])
        context = self.build()
        self.assertIsInstance(context[-1]['content'], list)
        image = next(part for part in context[-1]['content'] if part['type'] == 'image_url')
        decoded = Image.open(io.BytesIO(base64.b64decode(image['image_url']['url'].split(',', 1)[1])))
        try:
            self.assertEqual(decoded.size, (80, 60))
            self.assertGreater(decoded.convert('RGB').getpixel((20,20))[0], 240)
        finally:
            decoded.close()
        self.assertNotIn('当前模型不能看图', context[0]['content'])
        self.assertIn('实际图像', context[-1]['content'][0]['text'])
        self.assertNotIn('base64', json.dumps(self.store.message_sources([source])))

    def test_explicit_off_keeps_metadata_only_even_for_visual_model(self):
        self.settings.vision_mode = 'off'
        asset = self.picture()
        self.store.add_message(self.session, 'user', '看这张图片', attachments=[asset['id']])
        context = self.build()
        self.assertIsInstance(context[-1]['content'], str)
        self.assertNotIn('image_url', str(context))
        self.assertIn('未开启或未通过视觉输入检测', context[0]['content'])

    def test_recent_user_images_continue_into_followup_but_assistant_stickers_do_not(self):
        sent = self.picture('red')
        sticker = self.picture('blue')
        self.store.add_message(self.session, 'user', '看这张', attachments=[sent['id']])
        self.store.add_message(self.session, 'assistant', '收到', attachments=[sticker['id']])
        self.store.add_message(self.session, 'user', '刚才是什么颜色')
        context = self.build('刚才是什么颜色')
        images = [(m['role'], p) for m in context if isinstance(m['content'], list) for p in m['content'] if p['type'] == 'image_url']
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0][0], 'user')

    def test_inference_copy_is_resized_static_and_original_unchanged(self):
        asset = self.picture('red', (2200, 1400), animated=True)
        original = self.assets.path(asset['id']).read_bytes()
        url = self.assets.vision_url(asset['id'])
        with Image.open(io.BytesIO(base64.b64decode(url.split(',',1)[1]))) as image:
            self.assertEqual(max(image.size), 1280)
            self.assertEqual(getattr(image, 'n_frames', 1), 1)
            self.assertGreater(image.convert('RGB').getpixel((10,10))[0], 240)
        self.assertEqual(self.assets.path(asset['id']).read_bytes(), original)

    def test_small_context_reserves_image_cost_without_counting_base64_as_text(self):
        self.settings.context_tokens = 4096
        self.settings.reply_tokens = 512
        asset = self.picture()
        self.store.add_message(self.session, 'user', '图片是什么颜色？', attachments=[asset['id']])
        context = self.build()
        estimate = 0
        for message in context:
            if isinstance(message['content'], str):
                estimate += tokens(message['content']) + 16
            else:
                estimate += tokens(message['content'][0]['text']) + 16 + 1024
        self.assertLessEqual(estimate, 4096 - 512 - 512)

    def test_too_many_images_for_small_context_gives_actionable_error(self):
        self.settings.context_tokens = 4096
        self.settings.reply_tokens = 1024
        images = [self.picture(color)['id'] for color in ['red','blue','green','yellow']]
        self.store.add_message(self.session, 'user', '都看看', attachments=images)
        with self.assertRaisesRegex(ValueError, '减少图片数量|提高上下文容量'):
            self.build()

    def test_four_images_fit_larger_context_and_recent_repeats_are_deduplicated(self):
        images = [self.picture(color)['id'] for color in ['red','blue','green','yellow']]
        self.store.add_message(self.session, 'user', '这张', attachments=[images[0]])
        self.store.add_message(self.session, 'assistant', '收到')
        self.store.add_message(self.session, 'user', '一起看', attachments=images)
        context = self.build()
        self.assertEqual(sum(p['type']=='image_url' for m in context if isinstance(m['content'], list) for p in m['content']), 4)

    def test_old_images_outside_recent_window_and_archived_images_are_not_uploaded(self):
        asset = self.picture()
        self.store.add_message(self.session, 'user', '旧图', attachments=[asset['id']])
        for _ in range(8):
            self.store.add_message(self.session, 'user', '继续聊文字')
        self.assertTrue(all(isinstance(m['content'], str) for m in self.build()))
        self.store.add_message(self.session, 'user', '再看图', attachments=[asset['id']])
        self.assets.archive(asset['id'])
        self.assertTrue(all(isinstance(m['content'], str) for m in self.build()))

    def test_desktop_service_delivers_pixels_and_retry_uses_stored_attachment(self):
        core = Companion(self.settings, store=self.store, model=self.model)
        # This test owns the shared store; stop only the worker before tearDown closes it.
        core.maintain = lambda **kwargs: False
        try:
            asset = self.picture()
            core.send(self.session, '', attachments=[asset['id']])
            core.tasks.join()
            self.assertIsInstance(self.model.calls[-1][-1]['content'], list)
            reply = self.store.messages(self.session)[-1]
            self.store.finish_message(reply['id'], '部分回复', 'failed')
            core.retry(self.session)
            core.tasks.join()
            self.assertIsInstance(self.model.calls[-1][-1]['content'], list)
            self.assertEqual(sum(m['role']=='user' for m in self.store.messages(self.session)), 1)
        finally:
            core.stop.set()
            core.worker.join(timeout=3)

    def test_full_service_four_images_and_small_context_one_image(self):
        for capacity, count in [(4096,1),(8192,4)]:
            with self.subTest(capacity=capacity):
                self.settings.context_tokens = capacity
                images = [self.picture(color)['id'] for color in ['red','blue','green','yellow'][:count]]
                core = Companion(self.settings, store=self.store, model=self.model)
                core.maintain = lambda **kwargs: False
                try:
                    session = self.store.new_session()
                    core.send(session, '看看图片', attachments=images)
                    core.tasks.join()
                    self.assertEqual(self.store.messages(session)[-1]['status'], 'complete', core.last_chat_error)
                finally:
                    core.stop.set()
                    core.worker.join(timeout=3)


class VisionProbeTests(unittest.TestCase):
    def client(self, mode='auto'):
        return ModelClient(Settings(vision_mode=mode), env={'base_url':'http://localhost/v1','ds_model':'vision-test'})

    @staticmethod
    def recognize(messages, **kwargs):
        url = messages[0]['content'][1]['image_url']['url']
        colors = {(255,0,0):'red',(0,0,255):'blue',(0,180,0):'green',(255,255,0):'yellow',(160,0,200):'purple'}
        with Image.open(io.BytesIO(base64.b64decode(url.split(',',1)[1]))) as image:
            return json.dumps({'left':colors[image.getpixel((10,10))], 'right':colors[image.getpixel((150,10))]})

    def test_auto_probe_verifies_pixels_and_caches_success(self):
        client = self.client()
        with patch.object(client, 'complete', side_effect=self.recognize) as request:
            self.assertTrue(client.ensure_vision())
            self.assertTrue(client.ensure_vision())
            self.assertEqual(request.call_count, 1)
            self.assertTrue(client.vision_checked)

    def test_silent_text_only_gateway_does_not_pass_as_visual(self):
        client = self.client()
        with patch.object(client, 'complete', return_value='本鱼看不到图片，只收到了文字') as request:
            self.assertFalse(client.ensure_vision())
            self.assertFalse(client.ensure_vision())
            self.assertEqual(request.call_count, 1)
        self.assertIn('未通过', client.vision_status)

    def test_modes_on_off_skip_probe_but_manual_test_forces_real_verification(self):
        for mode, expected in [('on',True),('off',False)]:
            client = self.client(mode)
            with patch.object(client, 'complete', side_effect=self.recognize) as request:
                self.assertEqual(client.ensure_vision(), expected)
                request.assert_not_called()
                self.assertTrue(client.ensure_vision(force=True))
                self.assertEqual(request.call_count, 1)

    def test_unsupported_images_fall_back_but_auth_and_transient_errors_do_not(self):
        for message in ['模型服务 HTTP 400：不支持参数','模型服务 HTTP 422：参数错误']:
            client = self.client()
            with patch.object(client, 'complete', side_effect=ModelError(message)):
                self.assertFalse(client.ensure_vision())
        for message in ['模型服务 HTTP 401：认证失败','无法连接模型服务或请求超时']:
            client = self.client()
            with patch.object(client, 'complete', side_effect=ModelError(message)):
                with self.assertRaises(ModelError):
                    client.ensure_vision()
                self.assertFalse(client.vision_checked)

    def test_cancellation_does_not_cache_false_capability(self):
        client = self.client()
        cancel = threading.Event()
        def stopped(*args, **kwargs):
            cancel.set()
            return self.recognize(*args, **kwargs)
        with patch.object(client, 'complete', side_effect=stopped):
            with self.assertRaises(Cancelled):
                client.ensure_vision(cancel)
        self.assertFalse(client.vision_checked)

    def test_visual_draft_probe_does_not_change_live_model_or_create_messages(self):
        from whale_friend.config import ConnectionConfig
        with tempfile.TemporaryDirectory() as folder:
            core = Companion(Settings(data_dir=folder), model=FakeModel())
            try:
                with patch('whale_friend.service.ModelClient') as model:
                    model.return_value.ensure_vision.return_value = True
                    model.return_value.vision_status = '视觉测试通过'
                    model.return_value.model = 'draft-vision'
                    core.test_connection(ConnectionConfig('http://localhost/v1','secret-test','draft-vision'), vision=True)
                    core.tasks.join()
                event = next(e for e in list(core.events.queue) if e['kind']=='connection_test_done')
                self.assertTrue(event['vision_available'])
                self.assertFalse(core.store.sessions())
                self.assertIsInstance(core.model, FakeModel)
            finally:
                core.shutdown()


if __name__ == '__main__':
    unittest.main()
