"""Executable fixtures for R2 (density/tails/screen), R4-R8 items that are pure functions. Exit code != 0 on failure."""
import math, sys, ev_ref as E
rows={}
for l in open('chains.csv'):
    p=l.strip().split(','); rows.setdefault(p[0],[]).append((p[3],p[4],p[5],p[6]))
FAIL=[]
def check(name,cond,detail=''):
    print(('PASS ' if cond else 'FAIL ')+name+(' | '+detail if detail else ''))
    if not cond: FAIL.append(name)
def model(d,mod=None,tail_bins=20):
    r=list(rows[d])
    if mod: r=[mod(x) for x in r]
    q,rej=E.validate_quotes(r); F,flags,s=E.parity_forward(q); a,st,diag=E.reference_density(q,F,flags,tail_bins=tail_bins)
    return q,rej,F,flags,a,st,diag
DAYS=['2026-07-01','2026-08-12','2026-09-09','2026-09-23','2026-10-08']
# ---- R2.1 tails: endpoint premiums reproduced exactly; mass exact; support positive
for d in DAYS:
    q,rej,F,flags,a,st,diag=model(d)
    (Kl,pl),(Kr,cr)=diag['left_end'],diag['right_end']
    check(f'{d} endpoint put {Kl} reproduced',abs(E.ref_price(a,Kl,-1)-pl)<1e-9,f'{pl:.3f} -> {E.ref_price(a,Kl,-1):.6f}')
    check(f'{d} endpoint call {Kr} reproduced',abs(E.ref_price(a,Kr,1)-cr)<1e-9,f'{cr:.3f} -> {E.ref_price(a,Kr,1):.6f}')
    check(f'{d} mass==1, support>0, F_ref==F',abs(sum(p for s,p in a)-1)<1e-9 and min(s for s,p in a)>0 and abs(diag['F_ref_minus_F'])<1e-6)
# ---- R2.5 convergence: tail bins 20 -> 40 for EV over the k-set and ES
KSET=[0.566,0.786,0.328,0.768]
for d in DAYS:
    _,_,F,_,a20,_,_=model(d,tail_bins=20); _,_,_,_,a40,_,_=model(d,tail_bins=40)
    l20,l40=E.latent_bounds(a20),E.latent_bounds(a40); K0=round(F/50)*50; worst_ev=0; worst_es=0
    for legs in ([(-1,K0,1),(1,K0+400,1),(-1,K0,-1),(1,K0-400,-1)],[(-1,K0+200,1),(1,K0+400,1),(-1,K0-200,-1),(1,K0-400,-1)],[(-1,K0-100,-1),(1,K0-300,-1)],[(-1,K0+100,1),(1,K0+300,1)]):
        for k in KSET:
            r20,e20=E.ev_gross(l20,legs,k,0.6); r40,e40=E.ev_gross(l40,legs,k,0.6)
            worst_ev=max(worst_ev,65*abs((e20-r20)-(e40-r40)))
        # ES of P&L at exit over states (k=0.786), 201 states
        def es(lat):
            out=[]; sd=0.786*math.sqrt(0.6); ref=E.ev_gross(lat,legs,1.0,0.6)[0]
            for i in range(201):
                z=-6+12*i/200; w=z*sd; pw=math.exp(-z*z/2)
                v,_=E.state_values(lat,0.6,w,legs); out.append(((v-ref)*65,pw))
            t=sum(p for _,p in out); return E.es_partial([(L,p/t) for L,p in out])
        worst_es=max(worst_es,abs(es(l20)-es(l40)))
    check(f'{d} tail-bin convergence 20->40',worst_ev<=1.0 and worst_es<=10.0,f'max dEV Rs {worst_ev:.3f}, max dES Rs {worst_es:.3f}')
# ---- R2.3/R2.4 executable positive and negative controls (8 Oct chain)
def shift(K,ot,dlt):
    def f(x):
        if float(x[0])==K and x[1]==ot: return (x[0],x[1],float(x[2])+dlt,float(x[3])+dlt)
        return x
    return f
base=model('2026-10-08')
check('8-Oct clean chain: no parity flags, no screen flags',not base[3] and not base[6]['screen_flags'])
for K,ot,dl,expect in ((22300.0,'CE',100.0,True),(22600.0,'CE',2.0,True),(21800.0,'PE',1.5,True),(23000.0,'CE',1.0,True),(22600.0,'CE',0.5,False),(22300.0,'PE',100.0,True)):
    q,rej,F,flags,a,st,diag=model('2026-10-08',shift(K,ot,dl))
    flagged=(K in flags) or (diag and K in diag.get('screen_flags',[]))
    check(f'8-Oct inject {ot} {K:.0f} {dl:+} -> flagged={expect}',flagged==expect,f"parity_flag={K in flags} screen_flag={bool(diag) and K in diag.get('screen_flags',[])} status={st}")
q,rej=E.validate_quotes([(22300,'CE',5,4),(22350,'CE','nan',1),(22400,'XX',1,2),(22450,'PE',-1,2)])
check('validate: crossed/NaN/bad type/negative rejected',not q and sorted(r[2] for r in rej)==['BAD_TYPE','CROSSED_OR_EMPTY','CROSSED_OR_EMPTY','NOT_FINITE'],str(rej))
# ---- R7 structure identity
check('validate_structure mixed expiry',E.validate_structure([(-1,22300,1,'NF','2026-10-13'),(1,22700,1,'NF','2026-10-20')],'NF','2026-10-13')=='MIXED_INDEX_OR_EXPIRY')
check('validate_structure 1x2 ratio',E.validate_structure([(1,22300,1,'NF','e'),(-2,22400,1,'NF','e')],'NF','e')=='NAKED_OR_RATIO_SHORT')
check('validate_structure fractional qty',E.validate_structure([(0.5,22300,1,'NF','e')],'NF','e')=='BAD_QTY')
check('validate_structure IB ok',E.validate_structure([(-1,22300,1,'NF','e'),(1,22700,1,'NF','e'),(-1,22300,-1,'NF','e'),(1,21900,-1,'NF','e')],'NF','e')=='OK')
lo,hi=E.structural_range([(-1,22300,1),(1,22700,1),(-1,22300,-1),(1,21900,-1)])
check('structural_range IB400 payoff in [-400,0]',lo==-400 and hi==0,f'{lo},{hi}')
# ---- ES partial mass unit
es=E.es_partial([(-100,0.02),(-50,0.02),(0,0.96)],0.05)
check('ES partial mass',abs(es-(-100*0.02-50*0.02+0*0.01)/0.05)<1e-12,f'{es}')
# ---- R8 rate fixture: 8-Oct (Thu) entry -> 12-Oct (Mon) exit = 4 calendar days, r = 7%
q,rej,F,flags,a,st,diag=base; lat=E.latent_bounds(a); K0=round(F/50)*50
legs=[(-1,K0,1),(1,K0+400,1),(-1,K0,-1),(1,K0-400,-1)]
for k in (1.0,0.786):
    ref,exv=E.ev_gross(lat,legs,k,0.6)
    D=math.exp(-0.07*4/365)
    diff=65*abs(exv*(1-D))          # PV adjustment of the expected exit cash flow
    bound=0.07*4/365*400*65
    check(f'rate fixture k={k}: |EV(D=1)-EV(discounted)| <= r*days/365*max|net value|*lot',diff<=bound,f'diff Rs {diff:.2f}, bound Rs {bound:.2f}')
print('FAILURES:',FAIL); sys.exit(1 if FAIL else 0)
