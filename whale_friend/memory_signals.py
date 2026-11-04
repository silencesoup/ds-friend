"""Evidence-based recall cues, importance and reversible state transitions."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext

from .store import now

LEVELS = ('随口 / 玩笑','普通','重要','长期关键')
EXPLICIT = re.compile(r'换成|换回|改成|改回|改为|恢复为|更新为|更正为|现在(?:是|用|采用)|已经(?:完成|换|改)|取消了|不再')
UNCERTAIN = re.compile(r'如果|假如|假设|打算|准备|考虑|可能|也许|计划|想要|要不要|例如|比如|听说|他说|她说|[“”「」『』]|(?:没|没有|还没|并未|未能).{0,6}(?:换|改|完成|取消)')


def fingerprint(row):
    return hashlib.sha256(json.dumps([row['kind'],row['content'],row['updated_at']],ensure_ascii=False).encode()).hexdigest()


def phrases(raw,limit=8):
    if not isinstance(raw,list): return []
    return list(dict.fromkeys(word.strip() for word in raw if isinstance(word,str) and 1<=len(word.strip())<=20 and '\n' not in word))[:limit]


class MemorySignals:
    def __init__(self,hub):
        self.hub,self.store = hub,hub.store

    def enrich(self,rows):
        signals={row['memory_id']:row for row in self.store.rows('SELECT * FROM memory_signals')}
        for row in rows:
            signal=signals.get(row['id'])
            current=signal and (signal['manual'] or signal['fingerprint']==fingerprint(row))
            row['importance']=signal['importance'] if current else (0 if row['kind']=='角色约定' else 2 if row['kind'] in ('目标','相处偏好') else 1)
            row['importance_manual']=bool(signal and signal['manual'])
            row['importance_reason']=signal['reason'] if current else ''
            row['recall_cues']=json.loads(signal['cues']) if current and signal['fingerprint']==fingerprint(row) else []
        pending={key for row in self.store.rows("SELECT previous_id,newer_id FROM memory_changes WHERE status='pending'") for key in (row['previous_id'],row['newer_id'])}
        for row in rows: row['pending_change']=row['id'] in pending
        return rows

    def set_importance(self,identity,level):
        if level is not None and (type(level) is not int or not 0<=level<=3):
            raise ValueError('重要程度应为自动、随口、普通、重要或长期关键')
        rows=self.store.rows('SELECT * FROM memories WHERE id=?',(identity,))
        if not rows: raise ValueError('记忆不存在')
        with self.store.lock,self.store.db:
            if level is None:
                self.store.db.execute('UPDATE memory_signals SET manual=0,fingerprint=? WHERE memory_id=? AND manual=1',('',identity))
            else:
                self.store.db.execute('''INSERT INTO memory_signals VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(memory_id) DO UPDATE SET importance=excluded.importance,reason=excluded.reason,manual=1,updated_at=excluded.updated_at''',
                    (identity,level,'由用户指定', '[]',1,fingerprint(rows[0]),now()))
            self.store.db.execute('DELETE FROM memory_activations')
            self.store.db.execute('DELETE FROM memory_recall_traces')
            self.store.db.execute("UPDATE memory_topics SET dirty=1 WHERE id IN (SELECT topic_id FROM memory_topic_links WHERE memory_id=?)",(identity,))

    def topic_cues(self,topic):
        rows=self.store.rows('SELECT * FROM memory_topic_cues WHERE topic_id=?',(topic['id'],))
        return json.loads(rows[0]['cues']) if rows and rows[0]['fingerprint']==topic['fingerprint'] else []

    def apply_analysis(self,topic,result,members):
        """Called inside consolidation's fingerprint-checked transaction."""
        by_id={row['id']:row for row in members}
        annotations=result.get('annotations',[])
        if isinstance(annotations,list):
            for item in annotations[:12]:
                if not isinstance(item,dict) or type(item.get('id')) is not int: continue
                row=by_id.get(item['id'])
                level=item.get('importance')
                if not row or type(level) is not int or not 0<=level<=3: continue
                # Role-play trivia must not silently become a permanent user core fact.
                if row['kind']=='角色约定': level=min(level,1)
                if level==3:
                    sources=self.store.message_sources(json.loads(row['source_ids']))
                    declared=' '.join(m['content'] for m in sources if m['role']=='user' and m['status']=='complete')
                    if not re.search(r'很重要|最在意|一直|长期|务必|一定|不想|不希望|不要|底线|承诺',declared):
                        level=2
                self.store.db.execute('''INSERT INTO memory_signals VALUES(?,?,?,?,0,?,?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                    importance=CASE WHEN memory_signals.manual=1 THEN memory_signals.importance ELSE excluded.importance END,
                    reason=CASE WHEN memory_signals.manual=1 THEN memory_signals.reason ELSE excluded.reason END,
                    cues=excluded.cues,fingerprint=excluded.fingerprint,updated_at=excluded.updated_at''',
                    (row['id'],level,str(item.get('reason','按已存内容归纳，可人工调整'))[:80],json.dumps(phrases(item.get('cues',[]),4),ensure_ascii=False),fingerprint(row),now()))
        cues=phrases(result.get('recall_cues',[]))
        if cues:
            self.store.db.execute('INSERT OR REPLACE INTO memory_topic_cues VALUES(?,?,?,?)',
                                  (topic['id'],json.dumps(cues,ensure_ascii=False),topic['fingerprint'],now()))
        changes=result.get('changes',[])
        if isinstance(changes,list):
            for proposal in changes[:3]:
                if isinstance(proposal,dict): self.propose_change(proposal,by_id)
        self.store.db.execute("INSERT INTO meta VALUES('memory_hub_dirty','1') ON CONFLICT(key) DO UPDATE SET value='1'")

    def propose_change(self,proposal,allowed):
        old_id,new_id=proposal.get('previous_id'),proposal.get('newer_id')
        if type(old_id) is not int or type(new_id) is not int or old_id==new_id or old_id not in allowed or new_id not in allowed:
            return None
        old,new=allowed[old_id],allowed[new_id]
        existing=self.store.rows('SELECT id,status FROM memory_changes WHERE previous_id=? AND newer_id=?',(old_id,new_id))
        if existing and existing[0]['status']!='pending': return existing[0]['id']
        current={r['id'] for r in self.hub.records()}
        if old_id not in current or new_id not in current: return None
        ids=proposal.get('evidence_ids')
        sources=json.loads(new['source_ids'])
        if not isinstance(ids,list) or not ids or len(ids)>8 or any(type(i) is not int for i in ids) or not set(ids).issubset(sources):
            return None
        ids=list(dict.fromkeys(ids))
        messages=self.store.message_sources(ids)
        blocked={r['message_id'] for r in self.store.rows('SELECT message_id FROM forgotten_sources')}
        if len(messages)!=len(set(ids)) or any(m['role']!='user' or m['status']!='complete' or m['id'] in blocked for m in messages):
            return None
        old_sources=json.loads(old['source_ids'])
        if new['created_at']<old['created_at'] or (old_sources and min(ids)<=max(old_sources)):
            return None
        explanation=str(proposal.get('explanation','检测到可能的状态变化，请核对原文'))[:140]
        self.store.db.execute('''INSERT OR IGNORE INTO memory_changes(previous_id,newer_id,previous_fingerprint,newer_fingerprint,
            evidence_ids,explanation,status,created_at) VALUES(?,?,?,?,?,?,?,?)''',
            (old_id,new_id,fingerprint(old),fingerprint(new),json.dumps(ids),explanation,'pending',now()))
        change=self.store.rows('SELECT * FROM memory_changes WHERE previous_id=? AND newer_id=?',(old_id,new_id))[0]
        if change['status']!='pending': return change['id']
        if change['previous_fingerprint']!=fingerprint(old) or change['newer_fingerprint']!=fingerprint(new):
            return change['id']
        # Confidence alone cannot authorize replacing a fact. Require literal
        # old/new values and a shared entity in the cited user's completed change.
        subject,anchor=proposal.get('subject',''),proposal.get('anchor','')
        old_value,new_value=proposal.get('previous_value',''),proposal.get('newer_value','')
        raw='\n'.join(m['content'] for m in messages)
        markers=list(EXPLICIT.finditer(raw))
        marked=markers[0] if len(markers)==1 else None
        spans=all(isinstance(v,str) and 2<=len(v)<=60 for v in (subject,anchor,old_value,new_value))
        grounded=spans and subject in old['content'] and subject in new['content'] and subject in raw and anchor in old['content'] and anchor in new['content'] and anchor in raw
        grounded=grounded and old_value!=new_value and old_value in old['content'] and new_value in new['content'] and marked and new_value in raw[marked.end():marked.end()+80]
        if grounded:
            def entity_head(text):
                head=text.split(anchor,1)[0].replace(old_value,'').replace(new_value,'').replace('的','').strip()
                return re.sub(r'^(?:用户|我的|我把|目前|现在|以前|原来|之前|曾经|旧状态[：:]|新状态[：:])+','',head)
            generic={'用户','我','本鱼','鲸鱼娘','他','她','喜欢','偏好','状态','情况','工作','项目','设备','使用','希望'}
            grounded=subject not in generic and anchor not in generic and entity_head(old['content'])==entity_head(new['content'])
        if (self.hub.settings.memory_evolution_enabled and grounded and not UNCERTAIN.search(raw)
            and old['kind']!='角色约定' and new['kind']==old['kind']):
            self.resolve(change['id'],'accept')
        return change['id']

    def changes(self):
        rows=self.store.rows('''SELECT c.*,o.content AS previous_content,n.content AS newer_content
            FROM memory_changes c JOIN memories o ON o.id=c.previous_id JOIN memories n ON n.id=c.newer_id
            ORDER BY CASE c.status WHEN 'pending' THEN 0 ELSE 1 END,c.id DESC LIMIT 80''')
        for row in rows:
            row['evidence_ids']=json.loads(row['evidence_ids'])
            row['evidence']=[{'id':m['id'],'created_at':m['created_at'],'content':m['content'][:600]} for m in self.store.message_sources(row['evidence_ids'])]
        return rows

    def resolve(self,identity,decision):
        if decision not in ('accept','reject','undo'): raise ValueError('请选择确认、忽略或恢复旧状态')
        with self.store.lock, (nullcontext() if self.store.db.in_transaction else self.store.db):
            rows=self.store.rows('SELECT * FROM memory_changes WHERE id=?',(identity,))
            if not rows: raise ValueError('状态变化记录不存在')
            change=rows[0]
            if decision=='reject':
                if change['status']=='pending': self.store.db.execute("UPDATE memory_changes SET status='rejected',reviewed_at=? WHERE id=?",(now(),identity))
                return
            expected='applied' if decision=='undo' else 'pending'
            if change['status']!=expected: return
            records={r['id']:r for r in self.hub.records(include_inactive=True)}
            old,new=records.get(change['previous_id']),records.get(change['newer_id'])
            if not old or not new or fingerprint(old)!=change['previous_fingerprint'] or fingerprint(new)!=change['newer_fingerprint']:
                raise ValueError('原记忆已经修改，请重新核对后再更新状态')
            if decision=='accept' and (not old['active'] or not new['active']):
                raise ValueError('记忆有效状态已经变化，请重新核对')
            if decision=='undo' and (old['active'] or not new['active']):
                raise ValueError('存在后续状态变化，请先处理较新的变更')
            evidence=json.loads(change['evidence_ids'])
            messages=self.store.message_sources(evidence)
            blocked={r['message_id'] for r in self.store.rows('SELECT message_id FROM forgotten_sources')}
            if len(messages)!=len(evidence) or any(m['role']!='user' or m['status']!='complete' or m['id'] in blocked for m in messages):
                raise ValueError('变化依据已不再有效，请重新核对原始对话')
            inactive,active=(new,old) if decision=='undo' else (old,new)
            transfer=bool(inactive['pinned'])
            self.store.db.execute('''INSERT INTO memory_annotations VALUES(?,0,0,?,?) ON CONFLICT(memory_id)
                DO UPDATE SET pinned=0,active=0,updated_at=excluded.updated_at''',(inactive['id'],json.dumps(inactive['topics_override']) if inactive['topics_override'] is not None else None,now()))
            self.store.db.execute('''INSERT INTO memory_annotations VALUES(?,?,1,?,?) ON CONFLICT(memory_id)
                DO UPDATE SET pinned=MAX(memory_annotations.pinned,excluded.pinned),active=1,updated_at=excluded.updated_at''',
                (active['id'],int(transfer),json.dumps(active['topics_override']) if active['topics_override'] is not None else None,now()))
            self.store.db.execute('UPDATE memory_changes SET status=?,reviewed_at=? WHERE id=?',('undone' if decision=='undo' else 'applied',now(),identity))
            if inactive['importance_manual'] and not active['importance_manual']:
                self.store.db.execute('''INSERT INTO memory_signals VALUES(?,?,?,'[]',1,?,?) ON CONFLICT(memory_id)
                    DO UPDATE SET importance=excluded.importance,reason=excluded.reason,manual=1,updated_at=excluded.updated_at''',
                    (active['id'],inactive['importance'],'沿用原记忆的用户重要程度',fingerprint(active),now()))
            self.store.db.execute('DELETE FROM summaries')
            self.store.db.execute('DELETE FROM memory_activations')
            self.store.db.execute('DELETE FROM memory_recall_traces')
            self.store.db.execute("UPDATE memory_topics SET summary='',source_ids='[]',dirty=1 WHERE id IN (SELECT topic_id FROM memory_topic_links WHERE memory_id IN (?,?))",(old['id'],new['id']))
            self.store.db.execute("INSERT INTO meta VALUES('memory_hub_dirty','1') ON CONFLICT(key) DO UPDATE SET value='1'")
