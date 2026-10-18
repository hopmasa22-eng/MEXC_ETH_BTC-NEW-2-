import os, time, json, logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
load_dotenv()
BASE='https://contract.mexc.com'; TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
CHATS=[x.strip() for x in os.getenv('TELEGRAM_CHAT_IDS','').split(',') if x.strip()]
SYMBOLS=[x.strip().upper() for x in os.getenv('SYMBOLS','BTC_USDT,ETH_USDT').split(',') if x.strip()]
POLL=max(5,int(os.getenv('POLL_SECONDS','15'))); STATE_FILE=os.getenv('STATE_FILE','state.json')
TZ=ZoneInfo('Europe/Kyiv'); STEP=600000; log=logging.getLogger('two-rules')
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s | %(levelname)s | %(message)s')
s=requests.Session(); s.headers['User-Agent']='CryptoTwoRulesBot/1.0'
def nowms(): return int(time.time()*1000)
def col(c): return 'GREEN' if c['c']>=c['o'] else 'RED'
def fmt(ts): return datetime.fromtimestamp(ts/1000,timezone.utc).astimezone(TZ).strftime('%Y-%m-%d %H:%M')
def tg(text):
    if not TOKEN or not CHATS: log.warning('Telegram is not configured'); return
    for chat in CHATS:
        try:
            r=s.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':chat,'text':text},timeout=12); d=r.json()
            if r.ok and d.get('ok'): log.info('TELEGRAM SENT OK | chat=%s',chat)
            else: log.error('TELEGRAM ERROR | chat=%s | %s',chat,d)
        except Exception: log.exception('Telegram error | chat=%s',chat)
def load():
    try:
        with open(STATE_FILE,encoding='utf-8') as f: return json.load(f)
    except (OSError,ValueError): return {}
state=load()
def save():
    tmp=STATE_FILE+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(state,f,ensure_ascii=False,indent=2)
    os.replace(tmp,STATE_FILE)
def fetch(symbol):
    end=nowms()//1000; start=end-300*60
    r=s.get(f'{BASE}/api/v1/contract/kline/{symbol}',params={'interval':'Min1','start':start,'end':end},timeout=15); r.raise_for_status(); d=r.json()
    if d.get('success') is False or not isinstance(d.get('data'),dict): raise RuntimeError(f'Bad MEXC data: {d}')
    x=d['data']; out=[]
    for i,t in enumerate(x.get('time',[])):
        try: out.append({'ts':int(t)*1000,'o':float(x['open'][i]),'c':float(x['close'][i]),'h':float(x['high'][i]),'l':float(x['low'][i])})
        except (IndexError,TypeError,ValueError): pass
    out.sort(key=lambda c:c['ts']); groups={}
    for c in out:
        ts=c['ts']//STEP*STEP; g=groups.setdefault(ts,{'ts':ts,'o':c['o'],'c':c['c'],'h':c['h'],'l':c['l'],'last':c['ts']})
        g['h']=max(g['h'],c['h']); g['l']=min(g['l'],c['l'])
        if c['ts']>=g['last']: g['c']=c['c']; g['last']=c['ts']
    allc=sorted(groups.values(),key=lambda c:c['ts']); n=nowms()
    closed=[c for c in allc if c['ts']+STEP<=n][-100:]
    live=next((c for c in reversed(allc) if c['ts']+STEP>n),None)
    for c in closed: c['color']=col(c)
    if live: live['color']=col(live); live['close_ts']=live['ts']+STEP
    return closed,live
def opposite(c): return 'RED' if c=='GREEN' else 'GREEN'
def sid(symbol,rule,start,side): return f'{symbol}|{rule}|{start}|{side}'
def warning(symbol,rule,side):
    if rule==1: body='Свічка №7 закрилася потрібним кольором. Сигнал буде тільки після закриття свічки №8.'
    else: body='До закриття свічки №7 приблизно 5 хвилин. Попередження чинне, лише поки колір №7 залишається потрібним. Сигнал буде тільки після закриття №7.'
    tg(f'⚠️ ПОПЕРЕДЖЕННЯ — ПРАВИЛО {rule}\n\n{symbol.replace("_","")} Futures\nTimeframe: 10m\nНапрямок: {"LONG 🟢" if side=="LONG" else "SHORT 🔴"}\n{body}')
def signal(symbol,rule,side,ts):
    n=8 if rule==1 else 7
    tg(f'{"LONG 🟢" if side=="LONG" else "SHORT 🔴"}\n\n{symbol.replace("_","")} Futures\nTimeframe: 10m\nПравило: {rule}\nСвічка підтвердження: №{n}\nЧас: {fmt(ts)} (Київ)\n\nТрейдер Василь Павлів\nt.me/vasylpavliv')
def result(symbol,p,outcome,rel):
    maxn=15 if p['rule']==1 else 14
    tg(f'{outcome}\n{symbol.replace("_","")} Futures\nПравило: {p["rule"]}\nНапрямок: {"LONG 🟢" if p["side"]=="LONG" else "SHORT 🔴"}\nРезультатна свічка: {rel}/{maxn}\n\nТрейдер Василь Павлів\nt.me/vasylpavliv')
def candidates(candles,i):
    # i = index of #1. #1-#3 must be the final three candles of their color run.
    if i<1 or i+5>=len(candles): return []
    a=candles[i-1:i+6]; prior=a[0]['color']; seq=a[1:]
    ans=[]
    if prior=='RED' and [x['color'] for x in seq[:3]]==['GREEN']*3 and seq[3]['color']==seq[4]['color']=='RED': ans.append('LONG')
    if prior=='GREEN' and [x['color'] for x in seq[:3]]==['RED']*3 and seq[3]['color']==seq[4]['color']=='GREEN': ans.append('SHORT')
    return ans
def process(symbol):
    candles,live=fetch(symbol)
    if len(candles)<20: log.warning('Not enough candles | %s | %d',symbol,len(candles)); return
    st=state.setdefault(symbol,{'last':0,'setups':{}})
    if not st['last']:
        st['last']=candles[-1]['ts']; save(); log.info('INITIALIZED | %s | latest=%s %s',symbol,fmt(st['last']),candles[-1]['color']); return
    new=[c for c in candles if c['ts']>st['last']]
    for cur in new:
        relset=list(st['setups'].items())
        for key,p in relset:
            if p.get('done') or p.get('cancel'): continue
            rel=(cur['ts']-p['start'])//STEP; side=p['side']; wanted='GREEN' if side=='LONG' else 'RED'
            trigger=p['trigger']
            if p['rule']==1:
                if rel==7:
                    if cur['color']==trigger: p['warned']=True; warning(symbol,1,side)
                    else: p['cancel']=True; log.info('CANCEL R1 | %s | #7 mismatch',symbol)
                elif rel==8:
                    if p.get('warned') and not p.get('sent'): signal(symbol,1,side,cur['ts']); p['sent']=True
                elif rel>=9 and p.get('sent'):
                    if cur['color']==wanted: result(symbol,p,'WIN',rel); p['done']=True
                    elif rel==15: result(symbol,p,'LOSS',rel); p['done']=True
            else:
                if rel==7:
                    if cur['color']!=trigger: p['cancel']=True; log.info('CANCEL R2 | %s | #7 mismatch',symbol)
                    elif not p.get('sent'): signal(symbol,2,side,cur['ts']); p['sent']=True
                elif rel>=8 and p.get('sent'):
                    if cur['color']==wanted: result(symbol,p,'WIN',rel); p['done']=True
                    elif rel==14: result(symbol,p,'LOSS',rel); p['done']=True
        # Register candidates when #6 closes. Start is the candle before #1 and is not counted.
        idx=next((j for j,c in enumerate(candles) if c['ts']==cur['ts']),None)
        if idx is not None:
            i=idx-5
            for side in candidates(candles,i):
                start=candles[i]['ts']-STEP; trigger='GREEN' if side=='LONG' else 'RED'
                for rule in (1,2):
                    key=sid(symbol,rule,start,side)
                    if key not in st['setups']:
                        # Rule 1 needs #7 same as #6 (LONG green, SHORT red); Rule 2 needs #7 opposite.
                        trig=trigger if rule==1 else opposite(trigger)
                        st['setups'][key]={'rule':rule,'side':side,'start':start,'trigger':trig,'warned':False,'sent':False,'done':False,'cancel':False}
                        log.info('CANDIDATE R%d | %s | %s | start=%s',rule,symbol,side,fmt(start))
        st['last']=cur['ts']; save()
    if live:
        for p in st['setups'].values():
            if p['rule']!=2 or p.get('done') or p.get('cancel') or p.get('sent') or p.get('warned'): continue
            rel=(live['ts']-p['start'])//STEP
            left=(live['close_ts']-nowms())/1000
            if rel==7:
                if live['color']!=p['trigger']: p['cancel']=True; save()
                elif 240<=left<=330: warning(symbol,2,p['side']); p['warned']=True; save()
    log.info('HEARTBEAT OK | %s | closed_10m=%d | latest=%s %s | pending=%d',symbol,len(candles),fmt(candles[-1]['ts']),candles[-1]['color'],sum(1 for p in st['setups'].values() if not p.get('done') and not p.get('cancel')))
def main():
    log.info('Crypto Two-Rules Bot started | MEXC 10m | symbols=%s',','.join(SYMBOLS))
    log.info('R1: warn after #7 close, signal after #8 close, results #9-15 | R2: warn ~5m before #7 close, signal after #7 close, results #8-14')
    while True:
        for sym in SYMBOLS:
            try: process(sym)
            except Exception: log.exception('ERROR | %s',sym)
        time.sleep(POLL)
if __name__=='__main__': main()
