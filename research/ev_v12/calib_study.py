"""R3: calibration cohort on the engine's own convention (Gaussian-latent units), NIFTY, development block 18 Jun - 13 Oct 2026.
Observation per decision session d (12:30 poll, 2<=sessions_to_expiry<=4):
  planned exit session fixed at entry: d+2 if sessions_to_expiry>=3 else d+1 (no hindsight fallback);
  F_exit = parity_forward of the SAME expiry at the first 15:15-15:25 poll of the planned exit session;
  latent state w solves S_h(w) = F_exit under the entry-day reference model with tau from actual poll times;
  z = w / sqrt(tau);  r = |z| * sqrt(pi/2)   (E[r] = k when W_tau ~ N(0, k^2 tau)).
Expiry aggregate r_e = mean of r over completed sessions; ELIGIBLE iff >= 2 completed sessions (frozen rule).
Kalman local-level filter (slow h=10, fast h=2); R trained ONLY on the training block (expiries <= 2026-08-11), frozen."""
import math, datetime as dt, statistics as st, ev_ref as E
HOL={'2026-05-01','2026-05-28','2026-06-26','2026-09-14','2026-10-02','2026-10-20','2026-11-10','2026-11-24','2026-12-25'}
def is_session(d): return d.weekday()<5 and d.isoformat() not in HOL
def next_session(d,n=1):
    x=dt.date.fromisoformat(d) if isinstance(d,str) else d; c=0
    while c<n:
        x+=dt.timedelta(days=1)
        if is_session(x): c+=1
    return x.isoformat()
def sessions_between(a,b):
    x=dt.date.fromisoformat(a); e=dt.date.fromisoformat(b); n=0
    while x<e:
        x+=dt.timedelta(days=1)
        if is_session(x): n+=1
    return n
def tmins(day,hms,until_day,until_hm=(15,30)):
    """trading minutes from day hms to until_day until_hm, 09:15-15:30 sessions (375 min)."""
    h,m,s=[int(x) for x in hms.split(':')]; t0=h*60+m+s/60
    if day==until_day: return max(0.0,until_hm[0]*60+until_hm[1]-t0)
    mins=max(0.0,15*60+30-t0); x=day
    while True:
        x=next_session(x)
        if x==until_day: mins+=until_hm[0]*60+until_hm[1]-(9*60+15); break
        mins+=375
    return mins
# load entry chains
ent={}
for l in open('entry_all.csv'):
    p=l.strip().split(',')
    if len(p)<7: continue
    ent.setdefault(p[0],{'t':p[1],'e':p[2],'rows':[]})['rows'].append((p[3],p[4],p[5],p[6]))
ex={}
for l in open('exit_1515.csv'):
    p=l.strip().split(',')
    d=ex.setdefault(p[0],{'t':p[1],'e':p[2],'rows':[]})
    K=p[3]
    if p[4] and p[5]: d['rows'].append((K,'CE',p[4],p[5]))
    if p[6] and p[7]: d['rows'].append((K,'PE',p[6],p[7]))
obs=[]; sched={}
for d in sorted(ent):
    e=ent[d]['e']; ste=sessions_between(d,e)
    if not (2<=ste<=4): continue
    xs=next_session(d,2) if ste>=3 else next_session(d,1)
    rec={'d':d,'e':e,'ste':ste,'exit_session':xs,'status':None}
    sched.setdefault(e,[]).append(rec)
    q,_=E.validate_quotes(ent[d]['rows']); F,flags,s1=E.parity_forward(q)
    atoms,s2,diag=E.reference_density(q,F,flags)
    if atoms is None: rec['status']='ENTRY_'+s2; obs.append(rec); continue
    if xs not in ex or ex[xs]['e']!=e: rec['status']='EXIT_MISSING'; obs.append(rec); continue
    qx,_=E.validate_quotes(ex[xs]['rows']); Fx,_,s3=E.parity_forward(qx)
    if Fx is None: rec['status']='EXIT_NO_FORWARD'; obs.append(rec); continue
    tau=tmins(d,ent[d]['t'],xs,(int(ex[xs]['t'][:2]),int(ex[xs]['t'][3:5])))/tmins(d,ent[d]['t'],e)
    lat=E.latent_bounds(atoms)
    lo,hi=-8*math.sqrt(tau),8*math.sqrt(tau)
    f=lambda w: E.state_values(lat,tau,w,[])[1]-Fx
    if f(lo)>0 or f(hi)<0: rec['status']='EXIT_OUTSIDE_MODEL_RANGE'; obs.append(rec); continue
    for _ in range(60):
        mid=0.5*(lo+hi)
        if f(mid)<0: lo=mid
        else: hi=mid
    w=0.5*(lo+hi); z=w/math.sqrt(tau)
    strad=E.atm_straddle(q,F)
    rec.update(status='OK',F=F,Fx=Fx,tau=tau,z=z,r=abs(z)*math.sqrt(math.pi/2),r_old=abs(Fx-F)/(strad*math.sqrt(tau)))
    obs.append(rec)
ok=[o for o in obs if o['status']=='OK']
from collections import Counter
print('scheduled decision sessions',len(obs),'by status',dict(Counter(o['status'] for o in obs)))
print('OK obs: mean r (latent) %.3f | mean r_old (straddle sqrt tau) %.3f | corr-free check: mean z %.3f, mean z^2 %.3f'%(st.mean(o['r'] for o in ok),st.mean(o['r_old'] for o in ok),st.mean(o['z'] for o in ok),st.mean(o['z']**2 for o in ok)))
exps=sorted(sched)
agg=[]
for e in exps:
    v=[o['r'] for o in sched[e] if o['status']=='OK']
    agg.append((e,len(sched[e]),len(v),st.mean(v) if v else None))
elig=[(e,n,c,r) for e,n,c,r in agg if c>=2]
print('expiries',len(exps),'eligible(>=2 completed)',len(elig),' ineligible:',[(e,n,c) for e,n,c,r in agg if c<2])
train=[r for e,n,c,r in elig if e<='2026-08-11']
R=st.pvariance(train)
print('training block expiries',len(train),'R_train %.4f (sd %.3f)'%(R,math.sqrt(R)), ' full-block R %.4f'%st.pvariance([r for *_,r in elig]))
def kf(K):
    lam=K*K/(1-K); q=lam*R; k=1.0; P=0.15**2; path=[]
    for e,n,c,x in elig:
        path.append((e,k,math.sqrt(P)))
        Pm=P+q; G=Pm/(Pm+R); k+=G*(x-k); P=(1-G)*Pm
    return path,(k,math.sqrt(P))
for nm,K in (('slow',1-0.5**0.1),('fast',1-0.5**0.5)):
    path,fin=kf(K)
    print(nm,'final k %.3f sd %.3f'%fin,' prior-to-each-expiry path:',[round(k,3) for e,k,s in path])
import json
json.dump({'obs':obs,'agg':agg,'R_train':R},open('calib_study_out.json','w'),default=str,indent=0)
