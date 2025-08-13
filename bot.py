# bot.py — Mastermind Predator v4 (Paper only)
import sys, subprocess
try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

import time, json, math, csv, random
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional
from config import HEADERS, TRADING_BASE, DATA_BASE

TIMEFRAME="1Min"; SCAN_INTERVAL_SEC=5; MAX_SYMBOLS_PER_SCAN=150; MAX_CONCURRENT_POS=12
RISK_PCT_PER_TRADE=0.01; TP_PCT=0.007; SL_PCT=0.004; MIN_AVG_VOL=120_000; MIN_PRICE=1.0; MAX_PRICE=10000.0
DAILY_MAX_LOSS_PCT=0.06; EXTENDED_TRADING=True
PRE_MARKET_START=dtime(4,0); REGULAR_OPEN=dtime(9,30); REGULAR_CLOSE=dtime(16,0); AFTER_MARKET_END=dtime(20,0)
UNIVERSE=["AAPL","MSFT","NVDA","TSLA","AMD","META","AMZN","GOOGL","GOOG","NFLX","AVGO","SMCI","ASML","SHOP","UBER","CRM","ADBE","MU","INTC","COIN","PLTR","SQ","ABNB","DELL","ON","KLAC","LRCX","PANW","NOW","ANET","TTD","SNOW","MDB","BABA","PDD","NIO","LI","RIVN","CVNA","DDOG","CRWD","NET","ZS","OKTA","ARM","SOFI","UAL","JPM","BAC","CAT","GE","DE","MARA","RIOT","AFRM","WMT","TGT","HD","LOW","DAL","AAL","NKE","COST","PEP","KO","DIS","SBUX","BA","QCOM","TXN","MRVL","INTU","PYPL","NEE","ENPH","RUN","CCL","NCLH","GME","AMC","HOOD","SNAP","PINS","ROKU","DOCU","ZM","PATH","AI","IONQ","RBLX","U","TTWO","EA","BIDU","JD","NTES","TSM","GS","MS","C","USB","CAT","XOM","CVX","BP","T","VZ","F","GM","TM","RIVN","LCID"]

def _req(method,url,**kw):
    try:
        r=requests.request(method,url,headers=HEADERS,timeout=20,**kw); r.raise_for_status(); return r
    except Exception as e:
        body=getattr(getattr(e,'response',None),'text',''); print(f"{method} {url} -> {e} | {body[:140]}"); return None
def get_json(url,params=None):
    r=_req("GET",url,params=params or {}); return r.json() if r is not None else None
def post_json(url,payload):
    r=_req("POST",url,data=json.dumps(payload)); return r.json() if r is not None else None

def get_clock(): return get_json(f"{TRADING_BASE}/v2/clock")
def get_account(): return get_json(f"{TRADING_BASE}/v2/account")
def list_positions(): 
    res=get_json(f"{TRADING_BASE}/v2/positions"); return res if isinstance(res,list) else []
def get_bars(sym,tf,limit=90):
    res=get_json(f"{DATA_BASE}/v2/stocks/{sym}/bars",{"timeframe":tf,"limit":limit}); return res.get("bars") if res and "bars" in res else None
def get_snapshot(sym): return get_json(f"{DATA_BASE}/v2/stocks/{sym}/snapshot")

def is_open_regular():
    c=get_clock(); return bool(c and c.get("is_open",False))
def is_extended_now():
    now=datetime.now(ZoneInfo("America/New_York")).time()
    return (PRE_MARKET_START<=now<REGULAR_OPEN) or (REGULAR_CLOSE<=now<AFTER_MARKET_END)

def place_bracket(sym,qty,entry):
    extended=EXTENDED_TRADING and is_extended_now(); order_type="limit" if extended else "market"
    payload={"symbol":sym.upper(),"qty":qty,"side":"buy","type":order_type,"time_in_force":"day",
             "order_class":"bracket","take_profit":{"limit_price":round(entry*(1+TP_PCT),2)},
             "stop_loss":{"stop_price":round(entry*(1-SL_PCT),2)},"extended_hours":extended}
    if order_type=="limit": payload["limit_price"]=round(entry*1.002,2)
    return post_json(f"{TRADING_BASE}/v2/orders",payload)

def highest(vals,n): return max(vals[-n:]) if len(vals)>=n else float("nan")
def analyze(sym):
    bars=get_bars(sym,TIMEFRAME,60)
    if not bars or len(bars)<25: return None
    closes=[b["c"] for b in bars]; highs=[b["h"] for b in bars]; vols=[b["v"] for b in bars]
    last=closes[-1]
    if not (MIN_PRICE<=last<=MAX_PRICE): return None
    avg_vol=sum(vols[-20:])/20
    if avg_vol<MIN_AVG_VOL: return None
    hi20=highest(highs[:-1],20); vol_spike=vols[-1]>1.5*(sum(vols[-21:-1])/20); breakout=last>hi20 and vol_spike
    typical=[(b["h"]+b["l"]+b["c"])/3 for b in bars]
    vwap=(sum(typical[i]*vols[i] for i in range(-20,0))/max(1,sum(vols[-20:])))
    if not (breakout and last>vwap): return None
    snap=get_snapshot(sym) or {}
    if snap.get("trading_status") in {"Halted","T1"}: return None
    strength=(last-hi20)/max(0.01,last*0.01)+(last-vwap)/max(0.01,last*0.01)
    return {"symbol":sym,"entry":float(last),"strength":float(strength)}

def position_size(bp,entry):
    risk=max(0.0,bp)*RISK_PCT_PER_TRADE; per=max(0.01,entry*SL_PCT); shares=int(risk//per)
    if shares*entry>bp: shares=int(bp//entry)
    return max(0,shares)

def daily_loss_exceeded(acct):
    try:
        eq=float(acct.get("equity",0)); le=float(acct.get("last_equity",eq)); dd=(eq-le)/le if le else 0.0
        if dd<-DAILY_MAX_LOSS_PCT: print(f"🛑 Daily drawdown {dd:.2%} > {DAILY_MAX_LOSS_PCT:.0%} — pausing."); return True
    except: pass
    return False

def log_trade(row):
    new=False
    try: open("trades.csv","r").close()
    except FileNotFoundError: new=True
    with open("trades.csv","a",newline="") as f:
        w=csv.writer(f)
        if new: w.writerow(["ts","symbol","entry","qty","session","order_response"])
        w.writerow(row)

def keys_healthcheck(wait=15):
    while True:
        try:
            a=requests.get(f"{TRADING_BASE}/v2/account",headers=HEADERS,timeout=15)
            c=requests.get(f"{TRADING_BASE}/v2/clock",headers=HEADERS,timeout=15)
            print(f"🔑 Account={a.status_code} Clock={c.status_code}")
            if a.status_code==200 and c.status_code==200:
                print("✅ Keys OK, connected to Alpaca Paper Trading API."); return
            else: print("❌ Auth issue; retrying…")
        except Exception as e: print("Healthcheck error:",e)
        time.sleep(wait)

def scan_and_trade():
    acct=get_account()
    if not acct: print("❌ Cannot fetch account."); return
    if acct.get("trading_blocked"): print("🚫 Trading blocked."); return
    if daily_loss_exceeded(acct): return
    bp=float(acct.get("buying_power",0.0)); positions=list_positions()
    print(f"✅ BP ${bp:,.2f} | Pos {len(positions)}/{MAX_CONCURRENT_POS}")
    slots=max(0,MAX_CONCURRENT_POS-len(positions))
    if slots==0: return
    symbols=UNIVERSE[:MAX_SYMBOLS_PER_SCAN]; random.shuffle(symbols)
    ideas=[i for i in (analyze(s) for s in symbols) if i]
    if not ideas: print("🔎 No setups."); return
    ideas.sort(key=lambda d:d["strength"],reverse=True)
    taken=0
    for idea in ideas:
        if taken>=slots: break
        qty=position_size(bp,idea["entry"])
        if qty<=0:
            continue
        session="extended" if is_extended_now() else ("regular" if is_open_regular() else "unknown")
        print(f"🚀 {idea['symbol']} {session} | entry≈{idea['entry']:.2f} qty={qty}")
        resp=place_bracket(idea["symbol"],qty,idea["entry"])
        log_trade([datetime.now().isoformat(),idea["symbol"],idea["entry"],qty,session,json.dumps(resp) if resp else "err"])
        taken+=1

def main():
    print("🧠 Mastermind Predator v4 — PAPER ONLY — aggressive scalper (regular + extended hours).")
    keys_healthcheck()
    while True:
        try:
            if is_open_regular() or (EXTENDED_TRADING and is_extended_now()):
                scan_and_trade()
            else:
                now_et=datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S ET")
                print(f"⏸ Waiting for regular or extended session… ({now_et})")
            time.sleep(SCAN_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("👋 Stopped."); break
        except Exception as e:
            print("🔥 Loop error:",e); time.sleep(5)

if __name__=="__main__":
    main()
