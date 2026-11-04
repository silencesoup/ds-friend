import base64
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from tests.test_pet import fixture
from whale_friend.config import Settings
from whale_friend.model import ModelClient
from whale_friend.pet_capabilities import PetVoice, query_balance
from whale_friend.pet_updates import PetUpdates, unpack, version_key


def archive_for(root,version='0.4.2'):
    (root/'package.json').write_text(json.dumps({'name':'dsh-pet','version':version,'scripts':{'postinstall':'MUST_NOT_EXECUTE'}}))
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as tar:
        for path in root.rglob('*'):
            if path.is_file():
                tar.add(path,arcname='package/'+path.relative_to(root).as_posix())
    data = stream.getvalue()
    return data, {'version':version,'dist':{'tarball':'https://registry.npmjs.org/dsh-pet/-/dsh-pet-'+version+'.tgz','integrity':'sha512-'+base64.b64encode(hashlib.sha512(data).digest()).decode()}}


class PetCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(pet_voice_enabled=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_successful_update_and_rollback_preserve_external_source(self):
        old = fixture(self.root/'external')
        before = (old/'package.json').read_bytes()
        data,release = archive_for(fixture(self.root/'new'))
        manager = PetUpdates(self.root/'managed')
        with patch('whale_friend.pet_updates.download',return_value=data), patch('whale_friend.pet_support.LOCAL',self.root/'runtime'):
            target = manager.install(release,old,self.settings)
            self.assertEqual(manager.active(),target)
            self.assertEqual(json.loads((target/'package.json').read_text())['version'],'0.4.2')
            self.assertEqual(manager.rollback(self.settings),old.resolve())
            self.assertEqual(manager.active(),old.resolve())
        self.assertEqual((old/'package.json').read_bytes(),before)

    def test_download_corruption_or_incompatible_release_cannot_change_pointer(self):
        old = fixture(self.root/'external')
        manager = PetUpdates(self.root/'managed')
        data,release = archive_for(fixture(self.root/'new'))
        with patch('whale_friend.pet_updates.download',return_value=data+b'tamper'):
            with self.assertRaisesRegex(ValueError,'校验失败'):
                manager.install(release,old,self.settings)
        release['version']='0.5.0'
        with self.assertRaisesRegex(ValueError,'适配器'):
            manager.install(release,old,self.settings)
        self.assertFalse(manager.pointer.exists())

    def test_adapter_failure_preserves_previous_version(self):
        old = fixture(self.root/'external')
        new = fixture(self.root/'new')
        (new/'runtime/electron-helper/main.js').write_text('changed upstream protocol')
        data,release = archive_for(new)
        manager = PetUpdates(self.root/'managed')
        with patch('whale_friend.pet_updates.download',return_value=data), patch('whale_friend.pet_support.LOCAL',self.root/'runtime'):
            with self.assertRaisesRegex(ValueError,'启动协议'):
                manager.install(release,old,self.settings)
        self.assertFalse(manager.pointer.exists())

    def test_market_check_uses_highest_compatible_semver(self):
        versions = {v:{'name':'dsh-pet','version':v,'dist':{}} for v in ('0.4.2','0.4.10','0.5.0','0.4.11-beta.1')}
        metadata = {'dist-tags':{'latest':'0.5.0'},'versions':versions}
        with patch('whale_friend.pet_updates.download',return_value=json.dumps(metadata).encode()):
            result = PetUpdates(self.root/'managed').check(fixture(self.root/'old'))
        self.assertEqual(result['version'],'0.4.10')
        self.assertEqual(result['latest'],'0.5.0')
        self.assertTrue(result['available'])
        self.assertGreater(version_key('0.4.10'),version_key('0.4.9'))

    def test_archive_rejects_traversal_symlinks_and_windows_streams(self):
        for name,kind in [('package/../../escape',tarfile.REGTYPE),('package/link',tarfile.SYMTYPE),('package/x:ads',tarfile.REGTYPE),('package/a\\escape',tarfile.REGTYPE)]:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream,mode='w:gz') as tar:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '../../escape'
                tar.addfile(member)
            with self.assertRaises(ValueError):
                unpack(stream.getvalue(),self.root/'extract')
        self.assertFalse((self.root/'escape').exists())

    def test_voice_audio_cache_limits_and_no_partial_on_failure(self):
        voice = PetVoice(self.settings,self.root/'voice')
        calls = []
        async def synth(text,name,rate,path):
            calls.append((text,name,rate))
            path.write_bytes(b'ID3 synthetic audio')
        voice._synthesize = synth
        data = {'text':'Deep！Deep！'}
        first = voice.speak(data)
        self.assertEqual(voice.speak(data),first)
        self.assertEqual(len(calls),1)
        self.assertEqual(Path(first['file']).read_bytes(),b'ID3 synthetic audio')
        self.assertFalse(voice.speak({'text':'x'*501})['ok'])
        self.assertFalse(voice.speak({'text':'hi','voice':'not-a-voice'})['ok'])
        self.settings.pet_voice_enabled=False
        self.assertFalse(voice.speak(data)['ok'])

    def test_failed_synthesis_does_not_create_cache_hit(self):
        voice = PetVoice(self.settings,self.root/'voice')
        async def synth(text,name,rate,path):
            path.write_bytes(b'incomplete')
            raise OSError('private upstream error')
        voice._synthesize = synth
        result = voice.speak({'text':'你好'})
        self.assertFalse(result['ok'])
        self.assertNotIn('private',result['message'])
        self.assertEqual(list(voice.cache.iterdir()),[])

    def test_balance_multiple_wallets_exact_amounts_and_same_origin(self):
        model = ModelClient(self.settings,{'base_url':'http://127.0.0.1:9999/v1','api_key':'test-only'})
        raw = {'balance_infos':[{'currency':currency,'total_balance':'1.2300','granted_balance':'0','topped_up_balance':'1.2300'} for currency in ('CNY','USD')]}
        response = MagicMock()
        response.__enter__.return_value.read.return_value=json.dumps(raw).encode()
        opener = MagicMock()
        opener.open.return_value=response
        with patch('whale_friend.pet_capabilities.urllib.request.build_opener',return_value=opener):
            result = query_balance(model)
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['data']['wallets']),2)
        self.assertEqual(result['data']['wallets'][0]['total'],'1.2300')
        self.assertEqual(opener.open.call_args.args[0].full_url,'http://127.0.0.1:9999/v1/user/balance')

    def test_unsupported_balance_is_never_reported_as_zero(self):
        model = ModelClient(self.settings,{'base_url':'http://127.0.0.1:9999','api_key':'private'})
        with patch('whale_friend.pet_capabilities.urllib.request.build_opener',side_effect=OSError('secret=private')):
            result = query_balance(model)
        self.assertFalse(result['ok'])
        self.assertNotIn('data',result)
        self.assertNotIn('private',json.dumps(result))


if __name__=='__main__':
    unittest.main()
