"""R1 accuracy vs a time-consistent reference (Merton), new Gaussian-latent pricer fed ONLY quotes (Merton prices +/- 0.05)."""
import math, ev_ref as E
import a1_merton as M   # prints its own legacy table on import (P4 comparison) - kept for traceability
F=M.F; T=M.T
rows=[]
for K in range(23000,27050,50):
    for cp,ot in ((1,'CE'),(-1,'PE')):
        p=M.mprice(F,K,T,cp); rows.append((K,ot,max(p-0.05,0.0),p+0.05))
q,_=E.validate_quotes(rows); Fp,flags,st=E.parity_forward(q)
atoms,st2,diag=E.reference_density(q,Fp,flags)
print('synthetic Merton chain: status',st2,'F_ref-F %.3f'%(diag['F_ref']-F),'n strikes',diag['n_strikes'])
lat=E.latent_bounds(atoms)
structs=M.structs
print("tau | struct | truth k=.85 | latent k=.85 | err | truth k=1.15 | latent k=1.15 | err | latent k=1 (ref-exit)")
for tau in (0.5,0.6):
    for s,legs in structs.items():
        lg=[(-c,K,cp) for c,K,cp in legs]      # M.structs uses +1 = sold; convert to qty (+long/-short)
        t85=M.truth_ev(legs,0.85,tau); t115=M.truth_ev(legs,1.15,tau)
        def ev(k):
            ref,ex=E.ev_gross(lat,lg,k,tau)   # value of position (qty-signed): P&L = exit - entry(ref)
            return ex-ref
        e85,e115,e1=ev(0.85),ev(1.15),ev(1.0)
        assert abs(65*(e85-t85)) <= 50.0, (tau,s,'k=.85',65*(e85-t85))
        assert abs(65*(e115-t115)) <= 50.0, (tau,s,'k=1.15',65*(e115-t115))
        assert abs(65*e1) <= 1e-5, (tau,s,'k=1',65*e1)
        print(f"{tau} | {s:6} | {65*t85:8.1f} | {65*e85:8.1f} | {65*(e85-t85):+6.1f} | {65*t115:8.1f} | {65*e115:8.1f} | {65*(e115-t115):+6.1f} | {65*e1:+.2e}")
