import time, ev_ref as E
rows=[(p[3],p[4],p[5],p[6]) for p in (l.strip().split(',') for l in open('chains.csv')) if p[0]=='2026-10-08']
t0=time.perf_counter()
q,_=E.validate_quotes(rows); F,flags,s=E.parity_forward(q); atoms,st,diag=E.reference_density(q,F,flags)
t1=time.perf_counter()
pick,out=E.evaluate(q,F,flags,atoms,diag,0.6,[0.566,0.786,0.328,0.768])
t2=time.perf_counter()
# ES for top-50 eligible over 101 states at k_slow
import math
ok=[r for r in out if r['status']=='OK'][:50]; lat=E.latent_bounds(atoms)
for r in ok:
    sd=0.676*math.sqrt(0.6); res=[]
    ref=sum(n*E.ref_price(atoms,K,cp) for n,K,cp in r['legs'])
    for i in range(101):
        z=-5+10*i/100; v,_=E.state_values(lat,0.6,z*sd,r['legs']); res.append(((v-ref)*65,math.exp(-z*z/2)))
    t=sum(p for _,p in res); r['es5']=E.es_partial([(L,p/t) for L,p in res])
t3=time.perf_counter()
from collections import Counter
print('universe',len(out),'status',dict(Counter(r['status'] for r in out)))
print(f"timing desktop: quotes+density {1000*(t1-t0):.1f} ms | EV all candidates x4 k {1000*(t2-t1):.1f} ms | ES top-{len(ok)} x101 states {1000*(t3-t2):.1f} ms | total {1000*(t3-t0):.1f} ms")
print('pick:',pick and (pick['name'],round(pick['ev_robust']),{round(k,3):round(v) for k,v in pick['ev'].items()}))
top=sorted([r for r in out if r['status']=='OK'],key=lambda r:-r['ev_robust'])[:6]
for r in top: print(' ',r['name'],'EV_robust %.0f'%r['ev_robust'],'maxloss %.0f'%r['maxloss'],'ES5 %.0f'%r.get('es5',float('nan')))
