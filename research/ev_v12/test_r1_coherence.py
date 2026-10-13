"""R1 fixtures: statewise coherence of the Gaussian-latent Markov-functional exit pricer on the real chains,
the two Codex counterexample structures, tau boundaries, and k=1 zero (before costs, at the reference)."""
import math, ev_ref as E
rows={}
for l in open('chains.csv'):
    p=l.strip().split(','); rows.setdefault(p[0],[]).append((p[3],p[4],p[5],p[6]))
def statewise_values(lat, tau, w, strikes):
    sd=math.sqrt(max(0.0,1-tau)); P=[]
    for s,p,a,b in lat:
        pj=(1.0 if a<=w<b else 0.0) if sd==0 else E.Phi((b-w)/sd)-E.Phi((a-w)/sd)
        P.append((s,pj))
    C={K:sum(pj*max(s-K,0) for s,pj in P) for K in strikes}
    Pu={K:sum(pj*max(K-s,0) for s,pj in P) for K in strikes}
    Sh=sum(pj*s for s,pj in P); m=sum(pj for s,pj in P)
    return C,Pu,Sh,m
fails=0; checks=0
for d in sorted(rows):
    q,_=E.validate_quotes(rows[d]); F,flags,st=E.parity_forward(q)
    atoms,st2,diag=E.reference_density(q,F,flags)
    if atoms is None: print(d,'skipped:',st2,'(engine never runs on this chain)'); continue
    lat=E.latent_bounds(atoms)
    strikes=sorted(K for K in {k for k,_ in q} if abs(K-F)<=1000)
    worst=0.0
    for tau in (0.0,0.25,0.5,0.6,0.75,0.9,1.0):
        for z in (-4,-3,-2,-1,-0.5,0,0.5,1,2,3,4):
            w=z*math.sqrt(tau) if tau>0 else 0.0
            C,Pu,Sh,m=statewise_values(lat,tau,w,strikes)
            checks+=1
            assert abs(m-1)<1e-9
            for K1,K2 in zip(strikes,strikes[1:]):
                dK=K2-K1; v=C[K1]-C[K2]; vp=Pu[K2]-Pu[K1]
                worst=max(worst,v-dK,-v,vp-dK,-vp)
                if v>dK+1e-9 or v< -1e-9 or vp>dK+1e-9 or vp<-1e-9: fails+=1
            for K1,K2,K3 in zip(strikes,strikes[1:],strikes[2:]):
                if (C[K1]-C[K2])/(K2-K1) < (C[K2]-C[K3])/(K3-K2)-1e-9: fails+=1
            for K in strikes:
                if abs((C[K]-Pu[K])-(Sh-K))>1e-6: fails+=1
            if tau==1.0:
                for K in strikes:
                    sK=[s for s,p,a,b in lat if a<=w<b][0]
                    if abs(C[K]-max(sK-K,0))>1e-9: fails+=1
    # tau=0 equals reference; k=1 zero before costs
    for K in strikes[::5]:
        C0,P0,_,_=statewise_values(lat,0.0,0.0,[K])
        assert abs(C0[K]-E.ref_price(atoms,K,1))<1e-9
    K0=round(F/50)*50; zmax=0
    for legs in ([(-1,K0,1),(1,K0+400,1),(-1,K0,-1),(1,K0-400,-1)],[(1,K0-100,-1),(-1,K0-50,-1)],[(1,K0+50,1),(-1,K0+100,1)]):
        for tau in (0.25,0.5,0.6,0.75):
            ref,ex=E.ev_gross(lat,legs,1.0,tau); zmax=max(zmax,abs(ref-ex))
    print(d,f"states checked OK; max bound/convexity slack {worst:.2e}; k=1 |ref-E[exit]| max {zmax:.2e} pts")
print('statewise checks',checks,'violations',fails)
# Codex counterexamples under the new pricer
for d,(K1,K2,cp) in (('2026-07-01',(23450,23500,1)),('2026-10-08',(None,None,-1))):
    q,_=E.validate_quotes(rows[d]); F,flags,st=E.parity_forward(q); atoms,st2,diag=E.reference_density(q,F,flags); lat=E.latent_bounds(atoms)
    if K1 is None:
        K2=round(F/50)*50-200; K1=K2-50
    legs=[(1,K1,cp),(-1,K2,cp)] if cp==1 else [(1,K2,cp),(-1,K1,cp)]
    worst=-1e9
    for i in range(-400,401):
        w=i/100*math.sqrt(0.6); v,Sh=E.state_values(lat,0.6,w,legs); worst=max(worst,v)
    print(f"{d} {'call' if cp==1 else 'put'} vertical {K1}/{K2}: max statewise value over w in [-4,4] sd = {worst:.6f} (bound 50); strikes parity-flagged: {[K for K in (K1,K2) if K in flags]}")

assert fails == 0, f'coherence violations: {fails}'
assert checks == 385, f'fixture surfaces missing: {checks}'
