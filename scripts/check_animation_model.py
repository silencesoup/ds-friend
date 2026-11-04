"""One synthetic live-model animation check; never opens personal chat data."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from whale_friend.config import LOCAL,Settings
from whale_friend.service import Companion

folder=LOCAL/'animation-model-check'
settings=Settings(data_dir=str(folder/'data'),memory_tools_enabled=False,proactive_enabled=False,adaptive_enabled=False)
core=Companion(settings)
core.maintain=lambda **kwargs:False
inspection={}
stream=core.model.stream
def checked_stream(messages,cancel,**kwargs):
    instruction=messages[0]['content']
    inspection['protocol_in_context']='[[motion:' in instruction
    inspection['violin_offered']='小提琴演奏：' in instruction
    inspection['raw_marker']=False
    raw=''
    for chunk in stream(messages,cancel,**kwargs):
        raw+=chunk
        inspection['raw_marker']='[[motion:' in raw
        yield chunk
core.model.stream=checked_stream
try:
    session=core.store.new_session('动作协议 · 固定测试句')
    core.send(session,'本鱼，给我表演一下拉小提琴吧，只要一句轻松的回应。')
    deadline=time.monotonic()+160
    while core.chat_pending and time.monotonic()<deadline:
        time.sleep(.1)
    rows=core.store.messages(session)
    selected=core.store.rows('SELECT a.* FROM message_animations a JOIN messages m ON a.message_id=m.id WHERE m.session_id=?',(session,))
    report={'status':rows[-1]['status'],'clip':selected[-1]['clip_id'] if selected else None,
            'source':selected[-1]['source'] if selected else None,'marker_hidden':'[[motion:' not in rows[-1]['content'],
            'text_length':len(rows[-1]['content']),**inspection}
    (folder/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
finally:
    core.shutdown()
