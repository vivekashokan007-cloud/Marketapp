"""R4: fixed entry-time Rule B on the original synthetic structures (no hindsight fallback).
Horizon chosen from entry sessions-to-expiry BEFORE looking at outcomes: dte=2 -> N1C, dte>=3 -> N2C.
Inputs: hold extract S rows (c1..c10). Lots: illustrative 65 units (dated lot authority not established for the whole window)."""
import sys, glob, statistics as st
cols=['X0','N1O','N1E','N1C','N2C','N3C','XE']
S={}
for f in sorted(glob.glob(sys.argv[1]+'/c*.txt')):
    for l in open(f):
        p=l.strip().split(',')
        if p[0]=='S': S[tuple(p[1:5])]=p
for struct in ('IB400','IC200','IB200'):
    elig=[];done=[];miss=[]
    for k,p in sorted(S.items()):
        d,ix,tag,s=k
        if ix!='NF' or tag!='E' or s!=struct: continue
        dte=int(p[7])
        if not 2<=dte<=4: continue
        h='N1C' if dte==2 else 'N2C'
        v=dict(zip(cols,p[9:16]))[h]
        elig.append(d)
        if v=='': miss.append((d,dte,h))
        else: done.append((d,p[6],float(v)))
    vals=[v for _,_,v in done]
    print(f"{struct}: eligible {len(elig)} | completed {len(done)} | missing {len(miss)} {miss} | completed-case mean Rs {st.mean(vals):+.2f} median {st.median(vals):+.0f} win {100*sum(v>0 for v in vals)/len(vals):.0f}% worst {min(vals):.0f} | expiries {len({e for _,e,_ in done})}")
