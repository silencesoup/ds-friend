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
from tests.test_memory_actions import ActionModel
from whale_friend.config import Settings
from whale_friend.pet import callback_url
from whale_friend.pet_bridge import PetBridge, PREFIX
from whale_friend.pet_support import configuration, find_plugin, find_electron, jsonc, prepare_helper
from whale_friend.service import Companion


def fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'package.json').write_text('{"name":"dsh-pet","version":"0.4.1"}')
    (root / 'LICENSE').write_text('Copyright test author; MIT fixture')
    (root / 'runtime/electron-helper/main').mkdir(parents=True)
    (root / 'runtime/electron-helper/sprite').mkdir()
    (root / 'runtime/electron-helper/main.js').write_text("app.setName('dsh-pet-electron-helper');")
    (root / 'runtime/electron-helper/index.html').write_text('<html><head></head><body></body></html>')
    (root / 'runtime/electron-helper/main/bridge.js').write_text('setTimeout(() => {}, 45000);')
    (root / 'runtime/electron-helper/sprite/menu.js').write_text("{ label: '查看余额', action: 'show-balance' },\nS.displayMenuBranch(),\nif (leaf.action === 'open-site') { }", encoding='utf-8')
    (root / 'assets/webm').mkdir(parents=True)
    (root / 'assets/webm/idle.webm').write_bytes(b'test-animation')
    config = {'pets':[{'id':'original','name':'Original','size':462}], 'animations':{'idle':['idle'], 'sleep':[{'start':['sleep-start'],'loop':['sleep-loop'],'end':['sleep-end']}]}}
    (root / 'assets/config.jsonc').write_text('// source unchanged\n' + json.dumps(config))
    return root


class PetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plugin = fixture(self.root / 'plugin')
        self.settings = Settings(data_dir=str(self.root / 'data'), adaptive_enabled=False, proactive_enabled=False)
        self.model = FakeModel()
        self.core = Companion(self.settings, model=self.model)
        self.core.maintain = lambda **kwargs: False
        self.session = self.core.store.new_session()
        self.bridge = PetBridge(self.core, self.plugin, configuration(self.plugin,self.settings), self.session)

    def tearDown(self):
        self.bridge.stop()
        self.core.shutdown()
        self.temp.cleanup()

    def test_config_uses_existing_character_and_disables_separate_ai_loops(self):
        config = self.bridge.handle('GET', PREFIX+'config')['main']
        self.assertEqual(config['pets'][0]['name'], self.settings.character_name)
        self.assertEqual(config['pets'][0]['id'], 'main')
        self.assertFalse(config['pets'][0]['whisperEnabled'])
        self.assertFalse(config['pets'][0]['balanceEnabled'])
        self.assertFalse(config['trayEnabled'])
        self.assertEqual(jsonc((self.plugin/'assets/config.jsonc').read_text())['pets'][0]['id'], 'original')

    def test_pet_chat_shares_real_session_context_and_saved_messages(self):
        self.core.store.add_memory('相处偏好', '用户忙时少追问')
        result = self.bridge.handle('POST', PREFIX+'chat?pet=main&surface=desktop', {'text':'今天又要加班了'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.core.store.messages(self.session)[0]['content'], '今天又要加班了')
        self.assertIn('少追问', self.model.calls[-1][0]['content'])
        history = self.bridge.handle('GET', PREFIX+'chat?pet=main')
        self.assertEqual(history['messages'][-1]['content'], result['text'])
        self.assertEqual(self.bridge.state['pets']['main']['say']['data']['text'], result['text'])
        self.assertTrue(any(e['kind']=='user_saved' for e in list(self.core.events.queue)))

    def test_pet_chat_uses_same_immediate_memory_tools(self):
        model = ActionModel()
        model.store = self.core.store
        self.core.model = self.core.memory.model = self.core.memory_actions.model = model
        result = self.bridge.handle('POST', PREFIX+'chat?pet=main', {'text':'帮我记住，我喜欢海盐拿铁'})
        self.assertTrue(result['ok'], result)
        self.assertTrue(any('海盐拿铁' in row['content'] for row in self.core.store.memories()))

    def test_plugin_attachment_wire_format_is_saved_and_shared(self):
        buffer = io.BytesIO()
        Image.new('RGB',(30,20),'blue').save(buffer,'PNG')
        result = self.bridge.handle('POST', PREFIX+'chat?pet=main', {'text':'看图', 'attachment':{'data':base64.b64encode(buffer.getvalue()).decode(),'mediaType':'image/png'}})
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(self.core.store.messages(self.session)[0]['attachments']), 1)
        self.assertNotIn('base64', json.dumps(self.core.store.messages(self.session)))

    def test_normal_and_proactive_replies_reach_bubble_without_replaying_old_history(self):
        self.assertIsNone(self.bridge.state['pets']['main']['say']['data'])
        identity = self.core.store.add_message(self.session,'assistant','本鱼主动来陪你啦')
        self.bridge.refresh()
        self.assertEqual(self.bridge.state['pets']['main']['say']['data']['text'], '本鱼主动来陪你啦')
        counter = self.bridge.state['pets']['main']['say']['counter']
        self.bridge.refresh()
        self.assertEqual(self.bridge.state['pets']['main']['say']['counter'], counter)
        restarted = PetBridge(self.core,self.plugin,self.bridge.config,self.session)
        restarted.refresh()
        self.assertIsNone(restarted.state['pets']['main']['say']['data'])

    def test_long_poll_wakes_on_change_and_does_not_spin(self):
        done = threading.Event()
        outcome = []
        def wait():
            outcome.append(self.bridge.snapshot({'since':['0'],'timeout':['10000']}))
            done.set()
        thread = threading.Thread(target=wait)
        thread.start()
        self.bridge.write('main','say',{'text':'你好'})
        self.assertTrue(done.wait(1))
        thread.join()
        self.assertEqual(outcome[0]['pets']['main']['say']['data']['text'], '你好')

    def test_simultaneous_refresh_only_publishes_one_reply(self):
        self.core.store.add_message(self.session,'assistant','只显示一次的回信')
        threads = [threading.Thread(target=self.bridge.refresh) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.bridge.state['counter'], 2)
        self.assertEqual(self.bridge.state['pets']['main']['say']['data']['text'],'只显示一次的回信')

    def test_stop_releases_pending_state_requests(self):
        done = threading.Event()
        thread = threading.Thread(target=lambda:(self.bridge.snapshot({'since':['0'],'timeout':['10000']}),done.set()))
        thread.start()
        self.bridge.stop()
        self.assertTrue(done.wait(1))
        thread.join()

    def test_assets_are_scoped_and_no_global_profile_is_served(self):
        response = self.bridge.handle('GET', PREFIX+'thumb/main/idle.webm')
        self.assertEqual(Path(response['file']).read_bytes(), b'test-animation')
        for route in ('thumb/main/../../package.json','font/../../../../.credentials.yaml'):
            with self.assertRaises(ValueError):
                self.bridge.handle('GET', PREFIX+route)
        with self.assertRaises(ValueError):
            self.bridge.handle('GET','http://evil.example/'+PREFIX+'config')

    def test_callback_is_only_owned_loopback_response_endpoint(self):
        self.assertEqual(callback_url('http://127.0.0.1:1234/respond'), 'http://127.0.0.1:1234/respond')
        for url in ('http://evil.example/respond','http://localhost:1234/respond','http://127.0.0.1:1234/other','http://name:secret@127.0.0.1:1234/respond'):
            with self.assertRaises(ValueError):
                callback_url(url)

    def test_sleep_and_open_use_plugin_contract_and_tk_event_queue(self):
        self.assertTrue(self.bridge.handle('POST', PREFIX+'sleep?pet=main')['sleeping'])
        self.assertTrue(self.bridge.state['pets']['main']['sleep']['data']['sleeping'])
        self.assertFalse(self.bridge.handle('POST', PREFIX+'wake?pet=main')['sleeping'])
        self.bridge.handle('POST',PREFIX+'focus-session', {'sessionId':self.session})
        self.assertTrue(any(e['kind']=='pet_open' and e['session']==self.session for e in list(self.core.events.queue)))

    def test_history_delete_does_not_erase_existing_memories(self):
        self.core.store.add_message(self.session,'user','旧聊天不能丢')
        self.core.store.add_memory('事实','原始记忆')
        result = self.bridge.handle('DELETE',PREFIX+'chat?pet=main')
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.core.store.memories()),1)
        self.assertEqual(len(self.core.store.messages(self.session)),1)

    def test_prepared_renderer_isolated_attributed_and_source_unchanged(self):
        original = (self.plugin/'runtime/electron-helper/sprite/menu.js').read_text(encoding='utf-8')
        with patch('whale_friend.pet_support.LOCAL', self.root/'local'):
            target = prepare_helper(self.plugin)
            self.assertIn("action: 'show-balance'", (target/'helper/sprite/menu.js').read_text(encoding='utf-8'))
            self.assertTrue((target/'NOTICE.txt').is_file())
            self.assertTrue((target/'LICENSE.dsh-pet').is_file())
            self.assertEqual(prepare_helper(self.plugin),target)
        self.assertEqual((self.plugin/'runtime/electron-helper/sprite/menu.js').read_text(encoding='utf-8'), original)

    def test_balance_result_uses_shared_state_leaf_and_manual_flag(self):
        value = {'ok':True,'shape':'money','data':{'wallets':[{'currency':'CNY','total':'12.34','granted':'0','toppedUp':'12.34'}]}}
        with patch('whale_friend.pet_bridge.query_balance',return_value=value) as query:
            self.assertTrue(self.bridge.handle('POST',PREFIX+'balance')['ok'])
            self.bridge.handle('POST',PREFIX+'balance')
            self.assertEqual(query.call_count,1)
        self.assertTrue(self.bridge.state['sections']['balance']['data']['manual'])
        self.assertEqual(self.bridge.state['sections']['balance']['data']['data']['wallets'][0]['total'],'12.34')

    def test_voice_route_returns_audio_and_reply_marks_desktop_playback(self):
        with patch.object(self.bridge.voice,'speak',return_value={'file':'test.mp3','contentType':'audio/mpeg'}) as speak:
            self.assertEqual(self.bridge.handle('POST',PREFIX+'voice/speak',{'text':'你好'})['contentType'],'audio/mpeg')
            speak.assert_called_once_with({'text':'你好'})
        self.core.store.add_message(self.session,'assistant','本鱼来啦')
        self.bridge.refresh()
        self.assertEqual(self.bridge.state['pets']['main']['say']['data']['voiceTo'],'desktop')
        self.assertEqual(len(self.bridge.handle('GET',PREFIX+'voice/voices')['voices']),4)

    def test_jsonc_preserves_url_strings_and_escaped_quotes(self):
        self.assertEqual(jsonc('{/* a */ "url":"https://test/x//y", "s":"a\\\"/*b*/" // tail\n}'), {'url':'https://test/x//y','s':'a"/*b*/'})

    def test_missing_dependencies_fail_with_actionable_message(self):
        self.assertEqual(find_plugin(str(self.plugin)), self.plugin.resolve())
        with self.assertRaisesRegex(ValueError,'Electron'):
            find_electron(self.plugin, str(self.root/'missing.exe'))
        with self.assertRaisesRegex(ValueError,'dsh-pet'):
            find_plugin(str(self.root/'missing'))


if __name__=='__main__':
    unittest.main()
