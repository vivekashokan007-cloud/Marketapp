import copy
import json
import os
import tempfile
import unittest
from datetime import timedelta, datetime
from unittest import mock
import brain
import daily_review_v1 as dr
import index_authority_store as ias
import index_single_leg_v1 as sl
import single_leg_store_v1 as store
import learning_ledger_v1 as ledger
import session_evidence_v1 as se
import test_index_single_leg_v1_delivery_b as F
from test_single_leg_store_e2e_x1_identity import admissible_result, at, OK

class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sd = os.path.join(self.tmp.name, 'single_leg_v1')
        self.pos = store.admit(self.sd, admissible_result(), 'NF', clock=at(5), **OK)['position']
    def test_overdue_fresh_and_stale_preserve_contract(self):
        t = (F.D + timedelta(days=1)).replace(hour=9, minute=20)
        for age, event, status in [(5, 'EXIT', 'CLOSED'), (400, 'EXIT_PENDING', 'EXIT_PENDING')]:
            q = F.q(self.pos, 170, 171, t-timedelta(seconds=age))
            p,e = sl.evaluate_exit(self.pos,q,[],t)
            self.assertEqual((e,p['status'],p['exit_reason']),(event,status,'TIME_EXIT_OVERDUE'))
            self.assertEqual(p['exit_contract'],self.pos['exit_contract'])
    def test_existing_pending_reason_stays(self):
        t = F.D + timedelta(days=1)
        p,e = sl.evaluate_exit(dict(self.pos, status='EXIT_PENDING', exit_reason='TIME_EXIT'), F.q(self.pos,170,171,t),[],t)
        self.assertEqual(p['exit_reason'],'TIME_EXIT')
    def test_bad_entry_date_pending(self):
        p,e=sl.evaluate_exit(dict(self.pos,session_date='bad',entry_ts=None),{},[],F.D)
        self.assertEqual((e,p['exit_pending_reason']),('EXIT_PENDING','ENTRY_SESSION_UNAVAILABLE'))
    def test_cross_day_report_and_export(self):
        t = (F.D+timedelta(days=1)).replace(hour=9,minute=20)
        store.monitor(self.sd,{self.pos['instrument_key']:F.q(self.pos,90,91,t)}, {},clock=lambda:t)
        d1,d2=F.D.date().isoformat(),t.date().isoformat()
        self.assertEqual(store.outcomes(self.sd,d1),[])
        rows=store.outcomes(self.sd,d2)
        p,_=dr.partition_outcomes(d2,'NF',rows)
        r=p['index_single_leg_v1']['resolved'][0]
        self.assertTrue(r['cross_day_exit']);self.assertEqual(r['entry_session_date'],d1)
        export=se._single_leg(self.tmp.name,d2)
        self.assertEqual(len(export['closed']),1)
        self.assertEqual(export['closed'][0]['net_pnl'],r['recorded_net_pnl'])
    def test_old_entry_new_exit_retained(self):
        ps=[{'position_id':str(i),'status':'CLOSED','session_date':(F.D+timedelta(days=i)).date().isoformat()} for i in range(11)]
        doc={'positions':ps,'closed':[dict(ps[0],exit_ts='2026-11-13T09:20:00+05:30')],'history':[]}
        store._bound(doc);self.assertEqual(len(doc['closed']),1)

class Archive(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=self.tmp.name;self.sd=os.path.join(self.root,'index_authority_v1')
        self.rd=os.path.join(self.root,'self_study_v1')
        for i in range(10):
            day=(F.D-timedelta(days=i)).date().isoformat()
            rows={ix:[{'decision_id':ix+day,'index':ix,'published_at':day+'T10:00:20+05:30',
                'decision_poll_ts':day+'T10:00:00+05:30','policy_version':'index_direction_v1',
                'direction':{'value':'BULL'},'spot':{'asof':day+'T10:00:00+05:30','value':24000}}] for ix in ('NF','BNF')}
            ias.save(self.sd,day,{'history':rows})
    def test_completed_backfill_and_idempotence(self):
        import daily_review_ledger_v1 as dl
        dl.catch_up(self.sd,self.rd,F.D+timedelta(days=1),lambda s:'WRITTEN',max_runs=10)
        r=json.loads(brain.daily_review_catchup(self.sd,self.rd,int((F.D+timedelta(days=1)).timestamp()*1000)))
        self.assertEqual(len(r['evidence_capture']['written']),10)
        r=ledger.capture(self.root);self.assertEqual(len(r['unchanged']),10)
    def test_write_failure_preserves_prior(self):
        ledger.capture(self.root)
        path=os.path.join(self.root,ledger.NAME,F.D.date().isoformat()+'.json')
        before=open(path,'rb').read()
        doc=ias.load(self.sd,F.D.date().isoformat());doc['history']['NF'][0]['reason_codes']=['changed']
        ias.save(self.sd,F.D.date().isoformat(),doc)
        atomic=ledger._atomic_write
        def fail(p,data):
            if p==path:raise OSError('synthetic write failure')
            return atomic(p,data)
        with mock.patch.object(ledger,'_atomic_write',fail):
            r=ledger.capture(self.root)
        self.assertIn(F.D.date().isoformat(),r['errors'])
        self.assertEqual(open(path,'rb').read(),before)
        self.assertEqual(ledger.capture(self.root)['status'],'OK')
    def test_other_index_isolation(self):
        ledger.capture(self.root);day=F.D.date().isoformat()
        path=os.path.join(self.root,ledger.NAME,day+'.json')
        before=json.load(open(path))['by_index']['NF']
        doc=ias.load(self.sd,day);doc['history']['BNF'][0]['reason_codes']=['perturbed'];ias.save(self.sd,day,doc)
        ledger.capture(self.root)
        self.assertEqual(json.load(open(path))['by_index']['NF'],before)
    def test_refresh_join_is_context_only_and_admission_setting_is_own(self):
        day=F.D.date().isoformat()
        d=os.path.join(self.root,'single_leg_v1');os.makedirs(d)
        pos={'position_id':'refresh','index':'NF','session_date':day,'status':'OPEN',
             'entry_decision_id':'new-refresh-id','entry_decision_poll_ts':day+'T10:00:00+05:30',
             'admission_evidence':{'enabled':True}}
        with open(os.path.join(d,'positions.json'),'w') as f:json.dump({'positions':[pos]},f)
        ledger.capture(self.root)
        doc=json.load(open(os.path.join(self.root,ledger.NAME,day+'.json')))
        self.assertEqual(doc['admissions']['refresh']['association'],'POLL_CONTEXT_ONLY')
        self.assertTrue(doc['admissions']['refresh']['admission_evidence']['enabled'])
    def test_cap_failure_keeps_prior_capture(self):
        ledger.capture(self.root);day=F.D.date().isoformat()
        path=os.path.join(self.root,ledger.NAME,day+'.json');before=open(path,'rb').read()
        with mock.patch.object(ledger,'MAX_BYTES',100):
            r=ledger.capture(self.root)
        self.assertEqual(r['status'],'PARTIAL');self.assertEqual(open(path,'rb').read(),before)
    def test_archive_stays_exportable_after_authority_pruned(self):
        ledger.capture(self.root);day=F.D.date().isoformat()
        os.unlink(os.path.join(self.sd,day+'.json'))
        self.assertEqual(se.resolve_session(self.root,day),day)
        b,raw=se.build(self.root,day)
        self.assertEqual(b['manifest']['parts']['learning_ledger']['status'],'PRESENT')
        self.assertEqual(b['manifest']['parts']['authority_doc']['status'],'MISSING')
    def test_export_archive_part(self):
        ledger.capture(self.root)
        b,raw=se.build(self.root,F.D.date().isoformat())
        self.assertEqual(b['manifest']['parts']['learning_ledger']['status'],'PRESENT')

class Teacher(unittest.TestCase):
    def test_lineage_and_legacy(self):
        import test_evaluation_chain_quote_contract as T
        for authority, selector in [(False,'pc2_paper_primary_v7'),(True,'index_managed_v1')]:
            c=T.candidate()
            if authority:
                c.update(authorityPolicyVersion='index_direction_v1',indexDecisionId='NF-0930',constructionVersion='index_construction_v1')
            out=brain._eval_single_candidate(T.chain_rows(True),T.snapshot(c),c,brain._teacher_default_config())
            self.assertIsNotNone(out)
            self.assertEqual(out['policy_selector_version'],selector)
            if authority:
                self.assertEqual(out['indexDecisionId'],'NF-0930')
                self.assertIn('single-leg and NEUTRAL',out['evaluation_coverage'])
    def test_teacher_report_discloses_coverage(self):
        r=json.loads(brain.session_teacher_research_report('2026-11-02','[]','[]'))
        self.assertIn('Single-leg and NEUTRAL',r['evaluation_coverage'])
    def test_metrics_mixed_refused(self):
        with tempfile.NamedTemporaryFile(mode='w',suffix='.json') as f:
            json.dump([{'policy_selector_version':v} for v in ('index_managed_v1','pc2_paper_primary_v7')],f);f.flush()
            with self.assertRaisesRegex(ValueError,'MIXED'):
                brain.evaluation_policy_selector(f.name)
