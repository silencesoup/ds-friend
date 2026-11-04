from __future__ import annotations

import http.client
import io
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

from PIL import Image

from whale_friend.backup import Backups
from whale_friend.config import Settings
from whale_friend.mobile import COOKIE, MobileServer, Pairing
from whale_friend.service import Companion
from whale_friend.store import Store
from tests.test_core import FakeModel


class MobileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(data_dir=str(self.root / "data"), proactive_enabled=False, adaptive_enabled=False)
        self.core = Companion(self.settings, model=FakeModel())
        self.session = self.core.store.new_session("电脑上的会话")
        self.pairing = Pairing(self.root / "pairing.json")
        self.server = MobileServer(self.core, port=0, host="127.0.0.1", pairing=self.pairing).start()
        self.cookie = ""

    def tearDown(self):
        self.server.close()
        self.core.shutdown()
        self.temporary.cleanup()

    def request(self, method, path, data=None, raw=None, headers=None):
        headers = dict(headers or {})
        if self.cookie:
            headers["Cookie"] = self.cookie
        if data is not None:
            raw = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            body = response.read()
            result = json.loads(body) if "application/json" in response.getheader("Content-Type", "") else body
            return response.status, dict(response.getheaders()), result
        finally:
            connection.close()

    def pair(self):
        status, headers, data = self.request("POST", "/api/pair", {"code": self.pairing.code})
        self.assertEqual(status, 200)
        cookie = next(value for key, value in headers.items() if key.lower() == "set-cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.cookie = cookie.split(";", 1)[0]

    def send(self, text="手机上接着聊", request_id=None, images=None):
        data = {"session": self.session, "text": text, "images": images or [], "request_id": request_id or str(uuid.uuid4())}
        return self.request("POST", "/api/send", data)

    def test_static_page_is_public_but_all_private_data_requires_pairing(self):
        self.assertEqual(self.request("GET", "/")[0], 200)
        for path in ("/api/state", "/api/memories", "/api/journals", "/api/images", "/api/images/1"):
            self.assertEqual(self.request("GET", path)[0], 401)
        self.assertEqual(self.request("GET", "/static/.env")[0], 404)
        self.assertEqual(self.request("GET", "/docs")[0], 404)

    def test_animation_catalog_and_video_share_authenticated_service(self):
        self.assertEqual(self.request('GET','/api/animations')[0],401)
        self.pair()
        status,headers,catalog = self.request('GET','/api/animations')
        self.assertEqual(status,200)
        self.assertEqual(len(catalog['clips']),109)
        self.assertNotIn('path',catalog['clips'][0])
        status,headers,state = self.request('GET','/api/state?session='+self.session)
        self.assertTrue(state['animation']['enabled'])
        status,headers,video = self.request('GET',state['animation']['url'],headers={'Range':'bytes=0-63'})
        self.assertEqual(status,206)
        self.assertEqual(len(video),64)
        self.assertIn('video/webm',headers.get('content-type',''))
        self.assertIn("media-src 'self'",headers.get('content-security-policy',''))

    def test_importance_and_state_review_share_desktop_memory(self):
        self.core.maintain=lambda **kwargs:False
        self.settings.memory_evolution_enabled=False
        old_source=self.core.store.add_message(self.session,'user','测试小车摄像头使用USB接口')
        old=self.core.store.add_memory('事实','测试小车摄像头使用USB接口',[old_source])
        source=self.core.store.add_message(self.session,'user','测试小车摄像头已经换成CSI接口了')
        newer=self.core.store.add_memory('事实','测试小车摄像头使用CSI接口',[source])
        self.core.memory.hub.annotate(old,pinned=True)
        proposal={'previous_id':old,'newer_id':newer,'evidence_ids':[source]}
        with self.core.store.lock,self.core.store.db:
            change=self.core.memory.hub.signals.propose_change(proposal,{r['id']:r for r in self.core.store.memories()})
        self.assertEqual(self.request('POST',f'/api/memory/changes/{change}',{'decision':'accept'})[0],401)
        self.pair()
        self.assertEqual(self.request('PATCH',f'/api/memory/annotations/{old}',{'importance':3})[0],200)
        self.assertTrue(next(r for r in self.core.memory.hub.records() if r['id']==old)['pinned'])
        self.assertEqual(self.request('POST',f'/api/memory/changes/{change}',{'decision':'accept'})[0],200)
        rows={r['id']:r for r in self.core.memory.hub.overview()['records']}
        self.assertFalse(rows[old]['active'])
        self.assertTrue(rows[newer]['pinned'])
        self.assertEqual(rows[newer]['importance'],3)
        self.assertEqual(self.request('POST',f'/api/memory/changes/{change}',{'decision':'undo'})[0],200)
        self.assertTrue(next(r for r in self.core.memory.hub.records() if r['id']==old)['pinned'])

    def test_invalid_importance_does_not_partially_write_memory(self):
        self.pair()
        before=len(self.core.store.memories())
        self.assertEqual(self.request('POST','/api/memories',{'kind':'事实','content':'非法重要程度不应写入','importance':True})[0],400)
        self.assertEqual(len(self.core.store.memories()),before)

    def test_wrong_pairing_is_rate_limited_and_no_secret_echoed(self):
        for _ in range(8):
            self.assertEqual(self.request("POST", "/api/pair", {"code": "incorrect"})[0], 401)
        self.assertEqual(self.request("POST", "/api/pair", {"code": self.pairing.code})[0], 429)

    def test_other_origins_and_unexpected_hosts_are_rejected(self):
        self.pair()
        self.assertEqual(self.request("POST", "/api/sessions", {}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        status, headers, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        headers = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])

    def test_phone_and_desktop_share_messages_memory_and_context(self):
        self.pair()
        self.core.store.add_memory("偏好", "用户喜欢海盐拿铁")
        self.core.store.add_message(self.session, "user", "这条来自电脑")
        status, _, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["messages"][0]["content"], "这条来自电脑")
        self.assertEqual(self.send()[0], 200)
        self.core.tasks.join()
        messages = self.core.store.messages(self.session)
        self.assertEqual(messages[-2]["content"], "手机上接着聊")
        self.assertEqual(messages[-1]["status"], "complete")
        self.assertIn("用户喜欢海盐拿铁", self.core.model.calls[0][0]["content"])
        _, _, state = self.request("GET", "/api/state")
        self.assertEqual(state["messages"][-1]["content"], messages[-1]["content"])
        self.assertNotIn("api_key", json.dumps(state))
        self.assertTrue(any(event["kind"] == "user_saved" for event in list(self.core.events.queue)))

    def test_resending_same_request_does_not_duplicate_even_after_reload(self):
        self.pair()
        identity = str(uuid.uuid4())
        status, _, first = self.send(request_id=identity)
        self.assertEqual(status, 200)
        self.core.tasks.join()
        status, _, second = self.send(request_id=identity)
        self.assertEqual(status, 200)
        self.assertEqual(first, second)
        self.assertEqual(len(self.core.store.messages(self.session)), 2)
        self.assertEqual(self.send("另一段内容", request_id=identity)[0], 409)
        self.server.close()
        self.core.shutdown()
        self.core = Companion(self.settings, model=FakeModel())
        self.server = MobileServer(self.core, port=0, host="127.0.0.1", pairing=Pairing(self.root / "pairing.json")).start()
        self.assertEqual(self.send(request_id=identity)[0], 200)
        self.assertEqual(len(self.core.store.messages(self.session)), 2)

    def test_phone_can_add_and_correct_shared_facts(self):
        self.pair()
        self.assertEqual(self.request("POST", "/api/memories", {"kind": "偏好", "content": "喜欢蓝莓"})[0], 200)
        self.core.tasks.join()
        memory = self.core.store.memories()[0]
        self.assertEqual(memory["content"], "喜欢蓝莓")
        self.assertEqual(self.request("PATCH", f"/api/memories/{memory['id']}", {"kind": "偏好", "content": "喜欢草莓"})[0], 200)
        self.core.tasks.join()
        self.assertEqual(self.core.store.memories()[0]["content"], "喜欢草莓")
        _, _, rows = self.request("GET", "/api/memories")
        self.assertEqual(rows[0]["content"], "喜欢草莓")

    def test_phone_edits_core_topics_and_desktop_uses_them_next_turn(self):
        self.pair()
        status, _, created = self.request("POST", "/api/memories", {"kind":"事实", "content":"测试小车预算300元", "pinned":True, "active":True, "topics_override":["car"]})
        self.assertEqual(status, 200)
        self.core.tasks.join()
        identity = created["id"]
        self.assertEqual(self.core.memory.hub.core()[0]["id"], identity)
        self.assertEqual(self.send("你好呀")[0], 200)
        self.core.tasks.join()
        _, _, overview = self.request("GET", "/api/memory/overview?session=" + self.session)
        self.assertIn(identity, overview["activation"]["core_ids"])
        self.assertIn("测试小车预算300元", self.core.model.calls[0][0]["content"])
        self.assertEqual(self.request("PATCH", f"/api/memories/{identity}", {"kind":"事实", "content":"测试小车预算500元", "pinned":True, "active":True, "topics_override":["car"]})[0], 200)
        self.core.tasks.join()
        _, _, versions = self.request("GET", f"/api/memory/versions/{identity}")
        self.assertEqual(versions[0]["content"], "测试小车预算300元")
        status, _, topic = self.request("POST", "/api/memory/topics", {"title":"测试考研", "aliases":"背书，单词"})
        self.assertEqual(status, 200)
        self.core.tasks.join()
        self.assertTrue(any(row["id"] == topic["id"] for row in self.core.memory.hub.topics()))
        self.assertEqual(self.request("PATCH", f"/api/memory/annotations/{identity}", {"pinned":False, "active":False})[0], 200)
        self.core.tasks.join()
        self.assertFalse(self.core.memory.hub.core())
        self.assertEqual(self.core.store.memories()[0]["id"], identity)

    def test_memory_hub_auth_validation_and_metadata_preserve_originals(self):
        self.assertEqual(self.request("GET", "/api/memory/overview")[0], 401)
        self.assertEqual(self.request("POST", "/api/memory/organize", {})[0], 401)
        self.pair()
        user = self.core.store.add_message(self.session, "user", "测试原文")
        identity = self.core.store.add_memory("事实", "测试有来源的记忆", [user])
        before = self.core.store.memories()[0]
        self.assertEqual(self.request("PATCH", f"/api/memories/{identity}", {"kind":"事实", "content":before["content"], "pinned":True, "active":True})[0], 200)
        self.core.tasks.join()
        self.assertEqual(before, self.core.store.memories()[0])
        self.assertEqual(self.request("PATCH", f"/api/memories/{identity}", {"kind":"事实", "content":"不应写入", "pinned":True, "active":False})[0], 400)
        self.assertEqual(before, self.core.store.memories()[0])
        self.assertEqual(self.request("GET", "/api/memory/overview?session=missing")[0], 404)

    def test_chat_memory_execution_receipt_is_shared_with_phone(self):
        self.pair()
        proposal = {"action": "add", "kind": "偏好", "content": "用户喜欢海盐拿铁"}
        with patch.object(self.core.model, "complete", return_value=json.dumps(proposal, ensure_ascii=False)):
            self.assertEqual(self.send("帮我记住，我喜欢海盐拿铁")[0], 200)
            self.core.tasks.join()
        _, _, state = self.request("GET", "/api/state")
        self.assertEqual(state["messages"][0]["memory_action"]["status"], "saved")
        _, _, memories = self.request("GET", "/api/memories")
        self.assertEqual(memories[0]["content"], "用户喜欢海盐拿铁")
        self.assertEqual(state["messages"][0]["memory_action"]["memory_id"], memories[0]["id"])

    def test_phone_image_upload_caption_attachment_and_backup(self):
        self.pair()
        buffer = io.BytesIO()
        Image.new("RGB", (30, 20), "blue").save(buffer, "PNG")
        original = buffer.getvalue()
        status, _, asset = self.request("POST", "/api/images", raw=original, headers={"X-File-Name": quote("手机点头.png")})
        self.assertEqual(status, 200)
        self.core.tasks.join()
        self.assertFalse(asset["allow_reuse"])
        identity = asset["id"]
        self.assertEqual(self.request("PATCH", f"/api/images/{identity}", {"title": "点头", "description": "蓝色小鱼点头，表示开心", "tags": "点头，开心", "allow_reuse": True})[0], 200)
        self.core.tasks.join()
        self.assertEqual(self.send("这张图送给你", images=[identity])[0], 200)
        self.core.tasks.join()
        self.assertEqual(self.core.store.messages(self.session)[0]["attachments"][0]["title"], "点头")
        self.assertEqual(self.request("GET", f"/api/images/{identity}")[2], original)
        archive = self.core.backups.create(self.root / "backup")
        Backups.restore(archive, self.root / "restored")
        restored = Store(self.root / "restored")
        try:
            self.assertEqual(restored.messages(self.session)[0]["attachments"][0]["id"], identity)
        finally:
            restored.close()

    def test_phone_upload_reaches_visual_model_as_pixels_without_caption(self):
        self.pair()
        self.core.model.vision_available = True
        buffer = io.BytesIO()
        Image.new('RGB', (80, 60), 'red').save(buffer, 'PNG')
        status, _, asset = self.request('POST', '/api/images', raw=buffer.getvalue(), headers={'X-File-Name':'test.png'})
        self.assertEqual(status, 200)
        self.core.tasks.join()
        self.assertEqual(self.send('看这张图', images=[asset['id']])[0], 200)
        self.core.tasks.join()
        context = self.core.model.calls[-1]
        self.assertIsInstance(context[-1]['content'], list)
        self.assertTrue(any(p['type']=='image_url' for p in context[-1]['content']))
        _, _, state = self.request('GET', '/api/state')
        self.assertTrue(state['vision_enabled'])
        self.assertNotIn('base64', json.dumps(state))

    def test_journals_are_shared_and_invalid_inputs_do_not_write(self):
        self.pair()
        user = self.core.store.add_message(self.session, "user", "喜欢拿铁")
        self.core.store.add_journal("2026-11-03", "手机与电脑", "今天聊了拿铁。", [user], [])
        self.assertEqual(self.request("GET", "/api/journals")[2][0]["title"], "手机与电脑")
        self.assertEqual(self.request("POST", "/api/send", {"session": "missing", "request_id": str(uuid.uuid4())})[0], 404)
        self.assertEqual(self.request("POST", "/api/send", {"session": self.session, "request_id": "bad"})[0], 400)
        self.assertEqual(self.request("POST", "/api/images", raw=b"not an image")[0], 400)
        self.assertEqual(self.request("POST", "/api/send", raw=b"x" * 70000)[0], 413)

    def test_pairing_survives_restarts_and_can_be_revoked(self):
        self.pair()
        token = self.cookie.split("=", 1)[1]
        reloaded = Pairing(self.root / "pairing.json")
        self.assertTrue(reloaded.valid(token))
        self.assertNotIn(token, (self.root / "pairing.json").read_text())
        self.pairing.rotate()
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.pair()
        self.assertEqual(self.request("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/state")[0], 401)

    def test_phone_composing_suppresses_proactive_messages(self):
        self.pair()
        self.assertEqual(self.request("POST", "/api/activity", {"composing": True})[0], 200)
        self.assertGreater(self.core.remote_composing_until, time.monotonic())
        self.assertFalse(self.core.maybe_proactive())

    def test_history_is_paginated_without_losing_order(self):
        self.pair()
        for index in range(90):
            self.core.store.add_message(self.session, "user", str(index))
        _, _, state = self.request("GET", "/api/state?session=" + self.session)
        self.assertTrue(state["has_older"])
        self.assertEqual(len(state["messages"]), 80)
        first = state["messages"][0]["id"]
        _, _, old = self.request("GET", f"/api/state?session={self.session}&before={first}")
        self.assertEqual(len(old["messages"]), 10)
        self.assertEqual(old["messages"][0]["content"], "0")

    def test_stopped_port_can_be_started_again(self):
        port = self.server.port
        self.pair()
        self.request("GET", "/api/state")
        self.server.close()
        self.server = MobileServer(self.core, port=port, host="127.0.0.1", pairing=self.pairing).start()
        self.assertEqual(self.request("GET", "/api/state")[0], 200)
