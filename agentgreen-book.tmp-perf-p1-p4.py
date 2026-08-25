import json,time,uuid,httpx
API='http://127.0.0.1:8094'; JAVA='http://127.0.0.1:8080'
client=httpx.Client(timeout=30)
login=client.post(JAVA+'/api/v1/auth/login',json={'identifierType':'PHONE','identifier':'10001000000','password':'FanZK061345%'}); login.raise_for_status(); token=login.json()['token']['accessToken']; h={'Authorization':'Bearer '+token,'Content-Type':'application/json; charset=utf-8'}
conv=client.post(API+'/api/v1/agent/conversations',json={'title':'perf-p1-p4'},headers=h); conv.raise_for_status(); cid=conv.json()['conversation_id']
scenarios=[('P2','写一篇简短的 Java 学习帖子，只保存草稿'),('P3','写一篇简短的 Agent 学习帖子，明天下午2点发布'),('P4','搜索最近 Java 面试相关帖子，总结主要问题，基于结果写一篇短帖并保存草稿。')]
terminal={'COMPLETED','PARTIAL_SUCCESS','FAILED','CANCELLED','WAITING_USER','WAITING_HUMAN','WAITING_APPROVAL'}
for name,msg in scenarios:
    conv=client.post(API+'/api/v1/agent/conversations',json={'title':'perf-'+name+'-independent'},headers=h); conv.raise_for_status(); cid=conv.json()['conversation_id']
    started=time.perf_counter(); body=json.dumps({'content':msg,'client_timezone':'Asia/Shanghai'},ensure_ascii=False).encode('utf-8')
    accepted=client.post(f'{API}/api/v1/agent/conversations/{cid}/messages',content=body,headers={**h,'Idempotency-Key':uuid.uuid4().hex}); accepted.raise_for_status(); run=accepted.json()['run_id']; accepted_at=time.perf_counter()
    first=None; last=None
    while time.perf_counter()-started<180:
        rr=client.get(f'{API}/api/v1/agent/runs/{run}',headers=h)
        if rr.status_code==200:
            data=rr.json(); last=data
            if first is None and str(data.get('status','')).upper() in terminal: first=time.perf_counter()
            if first is not None: break
        time.sleep(.25)
    perf=(last or {}).get('performance') or {}; gap=(first-accepted_at)*1000 if first else None
    print(json.dumps({'scenario':name,'conversation_id':cid,'status':(last or {}).get('status'),'caller_latency_ms':round((first-started)*1000) if first else None,'completion_visible_gap_ms':round(gap) if gap is not None else None,'performance':perf,'run_id':run},ensure_ascii=False),flush=True)
