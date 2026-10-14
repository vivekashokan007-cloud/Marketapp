"""A1 part 3: Merton jump-diffusion as a time-consistent reference.
Q: sigma, lam, muJ, dJ. P(k): sigma*k, muJ*k, dJ*k, same lam, martingale drift  -> terminal centred log-return = k * Q's (exactly the engine's transform at tau=1).
Truth EV(tau) = entry_Q - E_P(k)[ sum c_i MertonQ(S_tau, K_i, T(1-tau)) ], with S_tau from the Merton P(k) law over tau*T.
Engine P4 sees only the Q terminal density + Q prices; horizon law = sqrt(tau)-scaled terminal law with k."""
import math
from a1_horizon import black, N, simpson_w, horizon_law, calib_vK, ev_P4, ev_P1
F,lot=25000.0,65
T=4.5/252          # ~4.5 trading sessions in trading-time years
sig,lam,muJ,dJ=0.085,10.0,-0.018,0.012
NMAX=8
def pois(n,m): return math.exp(-m)*m**n/math.factorial(n)
def comps(s,l,mu,d,t):
    kap=math.exp(mu+d*d/2)-1
    return [(pois(n,l*t), -l*kap*t - s*s*t/2 + n*mu, s*s*t + n*d*d) for n in range(NMAX)]
def mprice(f,K,t,cp,s=sig,l=lam,mu=muJ,d=dJ):
    tot=0.0
    for pn,m,v in comps(s,l,mu,d,t):
        if v<=1e-14: tot+=pn*max(cp*(f*math.exp(m)-K),0); continue
        sv=math.sqrt(v); d1=(math.log(f/K)+m+v)/sv; d2=d1-sv
        tot+=pn*(cp*(f*math.exp(m+v/2)*N(cp*d1)-K*N(cp*d2)))
    return tot
def mdens(x,cs):
    return sum(pn*math.exp(-(x-m)**2/(2*v))/math.sqrt(2*math.pi*v) for pn,m,v in cs)
def impl_var(f,K,price,cp):
    lo,hi=1e-10,0.05
    for _ in range(100):
        mid=(lo+hi)/2
        if black(f,K,mid,cp)<price: lo=mid
        else: hi=mid
    return (lo+hi)/2
# grids
n=2400; xs=[-0.4+i*0.8/n for i in range(n+1)]; sw=simpson_w(n,0.8/n)
csQ=comps(sig,lam,muJ,dJ,T)
ps=[mdens(x,csQ)*sw[i] for i,x in enumerate(xs)]; mass=sum(ps); ps=[p/mass for p in ps]
Ks=sorted({K for K in range(24200,25850,50)})
wK={K:impl_var(F,K,mprice(F,K,T,1 if K>=F else -1),1 if K>=F else -1) for K in Ks}
print(f"Merton Q: mass {mass:.6f}; ATM IV {math.sqrt(wK[25000]/T):.3f}; IV 24600 {math.sqrt(wK[24600]/T):.3f}; IV 25400 {math.sqrt(wK[25400]/T):.3f}")
structs={'IB400':[(1,25000,1),(-1,25400,1),(1,25000,-1),(-1,24600,-1)],
         'IC200':[(1,25200,1),(-1,25400,1),(1,24800,-1),(-1,24600,-1)],
         'BP200':[(1,24900,-1),(-1,24700,-1)],'BC200':[(1,25100,1),(-1,25300,1)],
         'BCD200':[(-1,25000,1),(1,25200,1)]}
def truth_ev(legs,k,tau):
    entry=sum(c*mprice(F,K,T,cp) for c,K,cp in legs)
    csP=comps(sig*k,lam,muJ*k,dJ*k,tau*T)
    pr=[mdens(x,csP)*sw[i] for i,x in enumerate(xs)]; ms=sum(pr); pr=[p/ms for p in pr]
    ex=sum(p*sum(c*mprice(F*math.exp(x),K,(1-tau)*T,cp) for c,K,cp in legs) for p,x in zip(pr,xs) if p>1e-12)
    return entry-ex
print("tau | struct | truth k=1 | truth k=.85 | P4 k=.85 | err | truth k=1.15 | P4 k=1.15 | err | P1 k=1 bias   (Rs/lot)")
for tau in (0.5,0.6):
    cache={}
    for s,legs in structs.items():
        entry=sum(c*mprice(F,K,T,cp) for c,K,cp in legs)
        for c,K,cp in legs:
            if (K,cp) not in cache: cache[(K,cp)]=calib_vK(F,K,cp,mprice(F,K,T,cp),xs,ps,tau,(1-tau)*wK[K])
        t1=truth_ev(legs,1.0,tau); t85=truth_ev(legs,0.85,tau); t115=truth_ev(legs,1.15,tau)
        e85=ev_P4(F,legs,xs,ps,cache,0.85,tau,entry); e115=ev_P4(F,legs,xs,ps,cache,1.15,tau,entry)
        p1=ev_P1(F,legs,xs,ps,wK,1.0,tau,entry)
        print(f"{tau} | {s:6} | {lot*t1:7.2f} | {lot*t85:8.1f} | {lot*e85:8.1f} | {lot*(e85-t85):+6.1f} | {lot*t115:8.1f} | {lot*e115:8.1f} | {lot*(e115-t115):+6.1f} | {lot*p1:7.1f}")
