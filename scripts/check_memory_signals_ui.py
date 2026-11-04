"""Desktop/mobile QA for recall, importance and state review using mock data."""
import json
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tests.test_core import FakeModel
from whale_friend.app import Desktop
from whale_friend.config import LOCAL,Settings,atomic_write
from whale_friend.mobile import MobileServer,Pairing
from whale_friend.service import Companion

BASE=LOCAL/'memory-signals-ui'
FOLDER=BASE/('run-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))


class SignalDemo(Desktop):
    def _start_mobile(self):
        self.mobile_server=MobileServer(self.core,port=0,host='127.0.0.1',pairing=Pairing(FOLDER/'pairing.json')).start()
        self.mobile_code.set(self.mobile_server.pairing.code)
        self.mobile_detail.configure(text='记忆升级 · 隔离测试服务')
        atomic_write(BASE/'connection.json',json.dumps({'url':f'http://127.0.0.1:{self.mobile_server.port}','code':self.mobile_server.pairing.code}))


if __name__=='__main__':
    settings=Settings(data_dir=str(FOLDER/'data'),mobile_enabled=True,adaptive_enabled=False,proactive_enabled=False,memory_evolution_enabled=False)
    core=Companion(settings,model=FakeModel())
    core.maintain=lambda **kwargs:False
    session=core.store.new_session('联想与状态变化 · 隔离测试')
    def fact(text,raw=None,kind='事实'):
        source=core.store.add_message(session,'user',raw or text)
        return core.store.add_memory(kind,text,[source]),source
    pressure,source=fact('测试用户曾因岗位被自动化替代而承受工作压力')
    core.memory.hub.signals.set_importance(pressure,2)
    old,source=fact('测试小车摄像头使用USB接口')
    newer,evidence=fact('测试小车摄像头使用CSI接口',raw='测试小车摄像头已经换成CSI接口了')
    fact('角色约定：本鱼爱吃白米饭，也会认真帮忙',kind='角色约定')
    proposal={'previous_id':old,'newer_id':newer,'evidence_ids':[evidence],'subject':'测试小车','anchor':'摄像头',
              'previous_value':'USB接口','newer_value':'CSI接口','explanation':'摄像头接口改变；演示待核对与恢复入口'}
    with core.store.lock,core.store.db:
        core.memory.hub.signals.propose_change(proposal,{r['id']:r for r in core.store.memories()})
    user=core.store.add_message(session,'user','今天好累')
    core.memory.hub.suggest(session,user,['work'],[pressure],'疲惫→以前明确说过的工作压力，当前原因待核对')
    core.memory.build_context(session,'今天好累',threading.Event())
    core.store.add_message(session,'assistant','本鱼在呢，先喘口气。今天累是工作上的事，还是单纯忙了一天呀？')
    app=SignalDemo(settings,core=core)
    app.base_title='记忆升级验证 · 隔离数据'
    app.title(app.base_title)
    app.after(300000,app._exit_app)
    app.mainloop()
