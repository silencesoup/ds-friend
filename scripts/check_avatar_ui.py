"""Desktop and mobile animation QA using disposable data and a mock model."""
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tests.test_core import FakeModel
from whale_friend.app import Desktop
from whale_friend.config import LOCAL, Settings, atomic_write
from whale_friend.mobile import MobileServer, Pairing
from whale_friend.service import Companion

FOLDER = LOCAL/'avatar-ui-check'


class AvatarModel(FakeModel):
    def stream(self,messages,cancel,**kwargs):
        self.calls.append(messages)
        content = messages[-1]['content']
        if '小提琴' in content:
            yield 'Deep！Deep！本鱼给你来一段小提琴表演，今天也一起好好摸鱼吧。[[motion:小提琴演奏]]'
        else:
            yield '哦鲸鲸！又被发现吃白饭啦。本鱼会陪你上班，也会认真帮你出主意的，哼哼。[[motion:吃白饭]]'


class AvatarDemo(Desktop):
    def _start_mobile(self):
        self.mobile_server = MobileServer(self.core,port=0,host='127.0.0.1',pairing=Pairing(FOLDER/'pairing.json')).start()
        self.mobile_code.set(self.mobile_server.pairing.code)
        self.mobile_detail.configure(text='动画隔离测试服务')
        atomic_write(FOLDER/'connection.json',json.dumps({'url':f'http://127.0.0.1:{self.mobile_server.port}','code':self.mobile_server.pairing.code}))


if __name__=='__main__':
    settings = Settings(data_dir=str(FOLDER/'data'),mobile_enabled=True,memory_tools_enabled=False,proactive_enabled=False,adaptive_enabled=False)
    core = Companion(settings,model=AvatarModel())
    core.maintain = lambda **kwargs:False
    if not core.store.sessions():
        core.store.new_session('角色动画 · 隔离测试')
        core.store.add_memory('相处偏好','测试用户喜欢安静陪伴')
    app = AvatarDemo(settings,core=core)
    app.base_title = '动画聊天验证 · 隔离数据'
    app.title(app.base_title)
    app.after(600000,app._exit_app)
    app.mainloop()
