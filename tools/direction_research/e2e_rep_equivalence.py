"""Per-replication equivalence of dr_e2e_sim.simulate_rep between ec11f65 and the R2 head (synthetic)."""
import importlib, json, sys
def load(root):
    for m in ('dr_core', 'dr_synth', 'dr_e2e_sim'):
        sys.modules.pop(m, None)
    sys.path.insert(0, root + '/tools/direction_research')
    mod = importlib.import_module('dr_e2e_sim')
    sys.path.pop(0)
    return mod
old, new = load(sys.argv[1]), load(sys.argv[2])
cfgs = [(150, 0.0, 0.0), (220, 0.1, 0.0), (150, 0.0, 0.3)]
diff_d = diff_rows = n = 0
for n_eval, skill, mom in cfgs:
    for i in range(int(sys.argv[3])):
        seed = 20261029 * 1000 + i
        a = old.simulate_rep(seed, n_eval, skill, mom); b = new.simulate_rep(seed, n_eval, skill, mom)
        n += 1
        diff_d += (a['d'], a['scheduled_until_nth'], a['ties']) != (b['d'], b['scheduled_until_nth'], b['ties'])
        diff_rows += a['n_rows'] != b['n_rows']
print(json.dumps({'reps_compared': n, 'reps_with_different_d_or_schedule': diff_d, 'reps_with_different_row_count': diff_rows}))
