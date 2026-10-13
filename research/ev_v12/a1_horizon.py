"""A1 research: horizon-pricing consistency.
Part 1: reproduce Codex's SVI slice result (pricer P1 = sqrt(tau)-scaled terminal law + sticky-strike Black, remaining var (1-tau)w).
Part 2: proposed pricer P4 = same horizon law, but per-strike remaining variance v_K calibrated so that
        E_q_tau[Black(S_tau,K,v_K)] = C_K(0)  (exact martingale consistency per contract at k=1).
Part 3: Merton jump-diffusion as a fully consistent reference ('truth'); compare engine variants vs truth for k != 1.
Zero rates (D=1). Pure Python. Reference/analysis code only, not app code."""
import math, sys
SQ2=math.sqrt(2.0)
def N(x): return 0.5*(1+math.erf(x/SQ2))
def black(f,K,v,cp):
    if v<=1e-14: return max(cp*(f-K),0.0)
    s=math.sqrt(v); d1=math.log(f/K)/s+s/2
    return cp*(f*N(cp*d1)-K*N(cp*(d1-s)))
def simpson_w(n,dx):
    w=[dx/3*(1 if i in (0,n) else (4 if i%2 else 2)) for i in range(n+1)]; return w
# ---------- generic engine pieces (operate on a terminal log-moneyness density on a grid) ----------
def horizon_law(xs,ps,k,tau):
    ex=sum(p*x for p,x in zip(ps,xs))
    y=[k*math.sqrt(tau)*(x-ex) for x in xs]
    z=sum(p*math.exp(v) for p,v in zip(ps,y))
    return [math.exp(v)/z for v in y]          # S_h / F, probabilities = ps
def ev_P1(F,legs,xs,ps,wK,k,tau,entry):
    sh=horizon_law(xs,ps,k,tau)
    ex=sum(p*sum(c*black(F*s,K,(1-tau)*wK[K],cp) for c,K,cp in legs) for p,s in zip(ps,sh))
    return entry-ex
def calib_vK(F,K,cp,C0,xs,ps,tau,v0):
    sh=horizon_law(xs,ps,1.0,tau)
    def f(v): return sum(p*black(F*s,K,v,cp) for p,s in zip(ps,sh))-C0
    lo,hi=1e-12,max(4*v0,1e-6)
    flo=f(lo)
    if flo>0: return None   # horizon law already too dispersed for this strike -> fail closed
    while f(hi)<0: hi*=2
    for _ in range(60):
        mid=0.5*(lo+hi)
        if f(mid)<0: lo=mid
        else: hi=mid
    return 0.5*(lo+hi)
def ev_P4(F,legs,xs,ps,vK,k,tau,entry):
    sh=horizon_law(xs,ps,k,tau)
    ex=sum(p*sum(c*black(F*s,K,vK[(K,cp)],cp) for c,K,cp in legs) for p,s in zip(ps,sh))
    return entry-ex
# ---------- Part 1/2: Codex SVI slice ----------
def part12():
    F,lot=25000.0,65
    a,b,rho,m,sig=0.00012,0.012,-0.45,0.0,0.035
    w=lambda x: a+b*(rho*(x-m)+math.sqrt((x-m)**2+sig*sig))
    def g(x):
        u=x-m;q=math.sqrt(u*u+sig*sig);v=w(x);wp=b*(rho+u/q);wpp=b*sig*sig/q**3
        return (1-x*wp/(2*v))**2-wp*wp/4*(1/v+0.25)+wpp/2
    n=4000; dx=2.0/n; xs=[-1+i*dx for i in range(n+1)]; sw=simpson_w(n,dx)
    ps=[g(x)/math.sqrt(2*math.pi*w(x))*math.exp(-0.5*(-x/math.sqrt(w(x))-math.sqrt(w(x))/2)**2)*sw[i] for i,x in enumerate(xs)]
    mass=sum(ps); fwd=sum(p*math.exp(x) for p,x in zip(ps,xs))
    ps=[p/mass for p in ps]
    structs={'IB400':[(1,25000,1),(-1,25400,1),(1,25000,-1),(-1,24600,-1)],
             'IC200':[(1,25200,1),(-1,25400,1),(1,24800,-1),(-1,24600,-1)],
             'BP200':[(1,24900,-1),(-1,24700,-1)],'BC200':[(1,25100,1),(-1,25300,1)]}
    wK={K:w(math.log(K/F)) for K in range(24400,25700,100)}
    print(f"SVI slice: raw mass {mass:.6f}, E[e^x] {fwd:.6f}, ATM vol(ann, T=5/365) {math.sqrt(w(0)/(5/365)):.3f}")
    print("tau | struct | P1 bias k=1 | P4 bias k=1 | P1(k=.92)-P1(1) | P4(k=.92) | P4(k=.81)   (Rs/lot, mids, no fees)")
    for tau in (0.25,0.5,0.6,0.75):
        cache={}
        for s,legs in structs.items():
            entry=sum(c*black(F,K,wK[K],cp) for c,K,cp in legs)
            for c,K,cp in legs:
                if (K,cp) not in cache: cache[(K,cp)]=calib_vK(F,K,cp,black(F,K,wK[K],cp),xs,ps,tau,(1-tau)*wK[K])
            p1=ev_P1(F,legs,xs,ps,wK,1.0,tau,entry); p1k=ev_P1(F,legs,xs,ps,wK,0.92,tau,entry)
            p4=ev_P4(F,legs,xs,ps,cache,1.0,tau,entry); p4k=ev_P4(F,legs,xs,ps,cache,0.92,tau,entry); p4k2=ev_P4(F,legs,xs,ps,cache,0.81,tau,entry)
            print(f"{tau:4} | {s:6} | {lot*p1:9.1f} | {lot*p4:9.3f} | {lot*(p1k-p1):9.1f} | {lot*p4k:9.1f} | {lot*p4k2:9.1f}")
if __name__=='__main__': part12()
