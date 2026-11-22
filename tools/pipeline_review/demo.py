"""Synthetic paired-index lifecycle and host-only performance evidence. No network."""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'app/src/main/python'), str(ROOT/'app/src/main/python/tests')]
import index_single_leg_v1 as sl
import index_authority_v1 as ia
import managed_evidence_v1 as me
import single_leg_store_v1 as store
import single_leg_readiness_v1 as ready
import index_authority_fixtures as F
import test_index_single_leg_v1_delivery_b as TB

D=TB.D
NOW=int(D.timestamp()*1000)


def setup(index):
    lot, spot, step, expiry = (65,24000,50,F.NF_EXPIRY) if index=='NF' else (30,52000,100,F.BNF_EXPIRY)
    chain=TB.chain(spot=spot,lot=lot,step=step)
    response=TB.contracts_response(chain)
    for row in response['data']:
        row.update(expiry=expiry,underlying_key=me.UNDERLYING_KEYS[index],lot_size=lot,minimum_lot=lot)
    contracts,rejected=me.parse_option_contracts(index,response)
    assert not rejected,rejected
    ev=me.build_context({index:contracts},{},[])
    ev['snapshots']=TB.snapshots(chain,gen=D,qty=500)
    decision={'decision_id':'synthetic_'+index,'analysis_ts':D.isoformat(),'session_date':F.SESSION,
              'availability':'AVAILABLE','direction':{'value':'BULL','er':.6}}
    p=sl.plan_entry(index,decision,chain,spot,session_date=F.SESSION,decision_dt=D,expiry=expiry,
                    captured_lot=lot,history=[],open_trades=[],evidence=ev)
    assert p['admit'],p['reasons']
    p.update(paper_enabled=True,decision_poll_ts=D.isoformat())
    return decision,p,chain


def main(output):
    with tempfile.TemporaryDirectory() as td:
        directory=os.path.join(td,'single_leg_v1')
        r={'direction_authority':{'status':'OK'},'by_index':{},'single_leg_by_index':{},
           'input_identity':{'market_collected_at_ms':NOW,'acquisition_id':'synthetic_pair'}}
        chains={}
        for ix in ('NF','BNF'):
            d,p,c=setup(ix);r['by_index'][ix]=d;r['single_leg_by_index'][ix]=p;chains[ix]=c
        started=time.perf_counter()
        request=ready.begin(directory,json.dumps(r),NOW)
        keys=json.loads(request)['keys']
        body={'status':'success','data':{}}
        for ix,c in chains.items():
            body['data'].update(TB.v3_response(c,D,qty=500)['data'])
        body['data']={k:v for k,v in body['data'].items() if v['instrument_token'] in keys}
        preflight=json.loads(ready.complete(directory,request,json.dumps(r),json.dumps(body),NOW-200,NOW+500,7,
                                            now_ms=NOW+1000,http_status=200))
        assert all(row['state']=='QUOTE_VALIDATED' for row in preflight['by_index'].values()),preflight
        preflight_ms=(time.perf_counter()-started)*1000
        admissions={}
        for ix in ('NF','BNF'):
            # New explicit-tap snapshot, not the preflight snapshot or a reused chain fill.
            p=r['single_leg_by_index'][ix]
            snap=TB.snapshots(chains[ix],gen=D+timedelta(seconds=2),qty=500)[p['instrument_key']]
            out=store.admit(directory,r,ix,clock=lambda:D+timedelta(seconds=5),snapshot=snap,
                            request_id='explicit_tap_'+ix,enabled=True,paper_mode=True,capital=250000,max_risk_pct=10)
            assert out['status']=='ADMITTED',out
            admissions[ix]={'decision_id':p['decision_id'],'instrument_key':p['instrument_key'],
                'request_id':out['request_id'],'status':out['status'],'position_id':out['position_id'],
                'holding_plan':out['position']['holding_plan']}
        t_exit=ia.parse_ts(F.SESSION+'T15:05:10+05:30')
        quotes={}
        for ix in ('NF','BNF'):
            p=r['single_leg_by_index'][ix]
            quotes.update(TB.snapshots(chains[ix],gen=t_exit-timedelta(seconds=1),qty=500))
        inputs=os.path.join(td,'exit.json')
        Path(inputs).write_text(json.dumps({'quotes':quotes,'clock':t_exit.isoformat()}))
        code='''import json,sys
import index_authority_v1 as ia
import single_leg_store_v1 as s
x=json.load(open(sys.argv[2]));d=sys.argv[1];now=lambda:ia.parse_ts(x['clock'])
before=s.read(d);out=s.monitor(d,x['quotes'],{},clock=now)
again=s.monitor(d,x['quotes'],{},clock=now)
assert len(out['events'])==2,out
assert all(e['event']=='EXIT' for e in out['events']),out
assert again['events']==[],again
print(json.dumps({'restart_loaded':len(before['positions']),'exit':out,'repeat':again}))'''
        child=subprocess.run([sys.executable,'-c',code,directory,inputs],capture_output=True,text=True,check=True,
                             env=dict(os.environ,PYTHONPATH=os.pathsep.join(sys.path[:2])),timeout=30)
        restart=json.loads(child.stdout)
        report={'synthetic_only':True,'live_broker_verified':False,'profitability_claim':'NONE',
            'readiness':preflight,'explicit_admissions':admissions,'restart':restart,
            'host_measurement':{'preflight_ms':round(preflight_ms,3),
                'readiness_bytes':os.path.getsize(os.path.join(directory,'quote_readiness.json')),
                'positions_bytes':os.path.getsize(os.path.join(directory,'positions.json')),
                'network_calls':0,'synthetic_get_batches':1},'pass':True}
        Path(output).write_text(json.dumps(report,indent=2))
        print(json.dumps({'pass':True,'output':output,'host_measurement':report['host_measurement']}))

if __name__=='__main__':
    main(sys.argv[1])
