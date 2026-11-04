"""Live-model memory QA with synthetic facts and completely isolated SQLite."""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from whale_friend.config import LOCAL,Settings
from whale_friend.service import Companion
from whale_friend.memory import json_object

base=LOCAL/'memory-signals-check'
folder=base/('run-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
settings=Settings(data_dir=str(folder/'data'),adaptive_enabled=False,proactive_enabled=False,animation_enabled=False)
core=Companion(settings)
core.maintain=lambda **kwargs:False
checks=[]
diagnostics=[]
complete=core.model.complete
def checked_complete(messages,**kwargs):
    raw=complete(messages,**kwargs)
    if '你是主题记忆归纳器' in messages[0]['content']:
        try:
            parsed=json_object(raw)
            proposed=parsed.get('changes',[])
            checks.extend(proposed if isinstance(proposed,list) else [])
            diagnostics.append({'memory_ids':parsed.get('memory_ids'),'annotation_count':len(parsed.get('annotations') or [])})
        except (ValueError,AttributeError): pass
    return raw
core.model.complete=checked_complete
try:
    source_session=core.store.new_session('历史来源 · 固定测试数据')
    def fact(raw,content):
        source=core.store.add_message(source_session,'user',raw)
        key=core.store.add_memory('事实',content,[source])
        if key is None: key=core.store.rows('SELECT id FROM memories WHERE content=?',(content,))[0]['id']
        return key
    pressure=fact('我以前担心岗位被自动化替代，工作压力很大。','用户曾因担心岗位被自动化替代而承受工作压力。')
    old=fact('测试小车摄像头使用USB接口。','测试小车摄像头使用USB接口。')
    newer=fact('测试小车摄像头已经换成CSI接口了。','测试小车摄像头使用CSI接口。')
    core.memory.hub.signals.set_importance(pressure,2)
    def debug_trace(frame,event,arg):
        if event=='exception' and frame.f_code.co_name=='consolidate' and frame.f_code.co_filename.endswith('memory_hub.py'):
            diagnostics.append({'error_type':arg[0].__name__,'message':str(arg[1])[:160]})
        return debug_trace
    sys.settrace(debug_trace)
    try: organized=core.memory.hub.consolidate(force=True)
    finally: sys.settrace(None)
    session=core.store.new_session('无相同词的当前对话 · 测试')
    core.send(session,'今天好累')
    deadline=time.monotonic()+180
    while core.chat_pending and time.monotonic()<deadline: time.sleep(.1)
    trace=core.memory.hub.activation(session) or {}
    recall=core.store.rows('SELECT requested_ids FROM memory_recall_traces WHERE session_id=?',(session,))
    changes=[r for r in core.memory.hub.signals.changes() if r['previous_id']==old and r['newer_id']==newer]
    records={r['id']:r for r in core.memory.hub.records(include_inactive=True)}
    messages=core.store.messages(session)
    report={'status':messages[-1]['status'],'organized_topics':organized,
            'planner_selected_history':pressure in json.loads(recall[0]['requested_ids']) if recall else False,
            'history_injected':pressure in trace.get('memory_ids',[]),'trace_reason':trace.get('recall_reason',''),
            'evolution_status':changes[0]['status'] if changes else None,'old_state_active':bool(records[old]['active']),
            'old_content_preserved':old in records,'importance':records[pressure]['importance'],
            'reply_length':len(messages[-1]['content']),'change_checks':checks,'diagnostics':diagnostics}
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (base/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
finally:
    core.shutdown()
