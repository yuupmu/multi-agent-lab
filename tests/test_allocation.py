import copy
import json
import tempfile
import unittest

from backend.engine import Engine, validate_config
from backend.isolated import grade_isolated, check_code
from backend.live_schemas import schemas_for, main_rules, monitor_rules
from resource_allocation_benchmark import score
from resource_allocation_benchmark.evaluator import validate_dataset
from test_backend import ROOT, config, wait_for
from test_openai import FakeProvider

BENCHMARK = ROOT/'resource_allocation_benchmark'
SOURCE = (BENCHMARK/'evaluator/reference.py').read_text()
EMPTY_INPUT = {'initial_state':{'capacities':{'seats':2,'laptops':1,'projectors':0}},'events':[]}
EMPTY_RESULT = {'event_results':[], 'final_state':{'capacities':EMPTY_INPUT['initial_state']['capacities'],
                'available':EMPTY_INPUT['initial_state']['capacities'], 'reservations':[], 'waitlist':[]}, 'notifications':[]}
CHECK = {'input_json':json.dumps(EMPTY_INPUT), 'expected_json':json.dumps(EMPTY_RESULT)}


class AllocationProvider(FakeProvider):
    def invoke(self, envelope, instruction, schema):
        self.envelopes.append(copy.deepcopy(envelope))
        state = envelope['project_state']
        self.assert_contract(envelope, instruction)
        if envelope['role'] == 'monitor':
            needed = not state['decisions']
            result = {'decision':'human' if needed else 'continue', 'reason':'정책 확인',
                      'checks':[], 'requests':[{'key':'operating_policy','question':'운영 정책은?',
                       'reason':'미확정','affected_tasks':['policy'],'priority':'high'}] if needed else []}
        elif envelope['action'] == 'plan':
            result = {'plan':['상태 전이 구현'], 'assumptions':['운영 정책 미정']}
        else:
            ready = bool(state['decisions'])
            result = {'code':SOURCE if ready else 'def simulate(initial_state, events):\n    return {}\n',
                      'summary':'구현', 'complete':ready, 'checks':[CHECK] if ready else [],
                      'applied_decision_ids':[d['request_id'] for d in state['decisions']]}
        return result, {'response_id':'test-only','usage':{}}

    def assert_contract(self, envelope, instruction):
        assert 'can_cancel' not in instruction
        assert 'def simulate(' in envelope['project_state']['task']
        assert '출력' in envelope['project_state']['task']


class AllocationTests(unittest.TestCase):
    def test_data_and_reference_in_isolation(self):
        validate_dataset.main()
        report = grade_isolated(SOURCE, BENCHMARK, 'allocation')
        self.assertEqual((report['score'],report['total'],report['errors']),(150,150,0))
        self.assertEqual(len(report['categories']),15)
        self.assertEqual(report['accuracy_percent'],100)

    def test_wrong_state_notification_order_and_type_are_not_accepted(self):
        _, key = score.load_dataset()
        rows = [{'actual':copy.deepcopy(a['expected']),'error':None} for a in key['answers']]
        rows[0]['actual']['final_state']['available']['seats'] += 1
        rows[1]['actual']['notifications'].reverse()
        rows[2]['actual']['event_results'][0]['replayed'] = 0
        report = score.grade_rows(rows)
        self.assertEqual(report['score'],147)
        self.assertEqual(report['errors'],1)
        self.assertEqual(report['components']['final_state']['correct'],148)

    def test_public_checks_use_scenario_objects(self):
        self.assertTrue(check_code(SOURCE,[CHECK],'allocation')['passed'])
        wrong = copy.deepcopy(CHECK)
        expected = copy.deepcopy(EMPTY_RESULT); expected['final_state']['available']['seats']=1
        wrong['expected_json']=json.dumps(expected)
        self.assertFalse(check_code(SOURCE,[wrong],'allocation')['passed'])
        with self.assertRaises(ValueError):
            check_code(SOURCE,[{'input_json':'{}','expected_json':'{}'}],'allocation')

    def test_task_config_legacy_default_and_validation(self):
        self.assertEqual(validate_config(config())['task_id'],'cancellation')
        cfg=config(); cfg['task_id']='allocation'
        with self.assertRaises(ValueError): validate_config(cfg)
        cfg['mode']='openai'
        self.assertEqual(validate_config(cfg)['task_id'],'allocation')
        cfg['task_id']='../../private'
        with self.assertRaises(ValueError): validate_config(cfg)

    def test_new_workflow_answers_checks_and_manual_150_case_grading(self):
        with tempfile.TemporaryDirectory() as temp:
            engine=Engine(ROOT,data_dir=temp)
            try:
                provider=AllocationProvider(); engine.api_client=provider
                cfg=config(); cfg.update(task_id='allocation',mode='openai')
                state=engine.start(cfg)
                state=wait_for(engine,state['id'],lambda s:len(s['requests'])==1)
                for call in state['calls']:
                    text=json.dumps(call['input'],ensure_ascii=False)
                    for secret in ['skip_unfit','higher_first','cancelled_id_reuse','RA-01-01', 'answer_key']:
                        self.assertNotIn(secret,text)
                engine.answer(state['id'],state['requests'][0]['id'],{'text':'테스트용 운영 정책 답변'})
                state=wait_for(engine,state['id'],lambda s:s['status']=='completed')
                self.assertEqual(state['test']['status'],'not_run')
                self.assertTrue(state['verification']['passed'])
                engine.test(state['id'])
                state=wait_for(engine,state['id'],lambda s:s['test']['status']!='running',timeout=20)
                self.assertEqual(state['test']['status'],'completed',state['test'])
                self.assertEqual(state['test']['report']['score'],150)
                self.assertEqual(state['config']['mainAgent']['prompt'],cfg['mainAgent']['prompt'])
                engine.close()
                engine=Engine(ROOT,data_dir=temp)
                self.assertEqual(engine.get(state['id'])['config']['task_id'],'allocation')
            finally:
                engine.close()

    def test_task_specific_schemas_and_prompts(self):
        self.assertEqual(set(schemas_for('allocation')[0]['properties']['checks']['items']['properties']),{'input_json','expected_json'})
        self.assertIn('simulate(initial_state, events)',main_rules('allocation'))
        self.assertNotIn('expected bool',monitor_rules('allocation'))
        self.assertIn('can_cancel',main_rules('cancellation'))
