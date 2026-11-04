"""Read-only account check, synthetic TTS and isolated official-package update test.

Does not instantiate Companion or open the user's SQLite database. --install
downloads an upstream renderer under .local/pet-capability-check only.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from whale_friend.config import LOCAL, Settings
from whale_friend.model import ModelClient
from whale_friend.pet_capabilities import PetVoice, query_balance
from whale_friend.pet_support import configuration, find_plugin, prepare_helper
from whale_friend.pet_updates import PetUpdates

parser = argparse.ArgumentParser()
parser.add_argument('--install',action='store_true')
args = parser.parse_args()
directory = LOCAL/'pet-capability-check'
directory.mkdir(parents=True,exist_ok=True)
settings = Settings(pet_voice_enabled=True)
report = {}
balance = query_balance(ModelClient(settings))
report['balance'] = {'ok':balance['ok'], 'wallet_count':len(balance.get('data',{}).get('wallets',[]))}
voice = PetVoice(settings,directory/'voice')
audio = voice.speak({'text':'Deep！Deep！本鱼的声音来啦，今天也想陪你一起摸鱼。'})
report['voice'] = {'ok':'file' in audio, 'bytes':Path(audio['file']).stat().st_size if 'file' in audio else 0}
source = find_plugin()
prepare_helper(source)
report['source_version'] = json.loads((source/'package.json').read_text(encoding='utf-8'))['version']
manager = PetUpdates(directory/'plugins')
try:
    release = manager.check(source)
    report['update'] = {key:value for key,value in release.items() if key!='dist'}
    if args.install:
        target = manager.install(release,source,settings)
        report['installed_version'] = json.loads((target/'package.json').read_text(encoding='utf-8'))['version']
        report['config_voice'] = configuration(target,settings)['main']['voiceEnabled']
        report['rollback_ok'] = manager.rollback(settings)==source.resolve()
except Exception as error:
    report['update_error'] = str(error)
(directory/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
