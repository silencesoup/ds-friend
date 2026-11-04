"""Verify 109 clip descriptions and generate contact sheets for visual review."""
import json
import sys
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from whale_friend.animations import CATALOG
from whale_friend.avatar_ui import video_frames
from whale_friend.config import LOCAL,ROOT

folder = LOCAL/'animation-review'
folder.mkdir(parents=True,exist_ok=True)
catalog = json.loads(CATALOG.read_text(encoding='utf-8'))['clips']
font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',15)
report = []
for page,start in enumerate(range(0,len(catalog),30)):
    sheet = Image.new('RGB',(1300,1110),'#f1f6fd')
    draw = ImageDraw.Draw(sheet)
    for index,clip in enumerate(catalog[start:start+30]):
        path = ROOT/'vendor/dsh-pet/upstream/assets/webm'/clip['file']
        frames = video_frames(path,256,144)
        alpha = None
        try:
            for number,(image,delay) in enumerate(frames):
                if number==0: alpha = image.getchannel('A').getextrema()
                if number>=36: break
        finally:
            frames.close()
        x,y = index%5*260,index//5*185
        sheet.paste(image,(x+2,y+2),image)
        draw.text((x+8,y+146),clip['id'],font=font,fill='#233b56')
        draw.text((x+8,y+167),'模型可选' if clip['model_selectable'] else '系统事件',font=font,fill='#7b8da3')
        report.append({'id':clip['id'],'alpha':alpha,'description':bool(clip['description']),'decoded':True})
    sheet.save(folder/f'contact-{page+1}.png')
(folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'described':len(report),'decoded':sum(r['decoded'] for r in report),'transparent':sum(r['alpha'][0]==0 for r in report)},ensure_ascii=False))
