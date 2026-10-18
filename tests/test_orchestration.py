from dataclasses import asdict
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from utils.config import Config
from utils.run_store import RunStore
from orchestration import start_run, scanner_handler, finalize_run


def event():
    return {"run_id": "run", "account_id": "123456789012", "config": asdict(Config.load()), "scanner": "scan_unattached_eips"}


def test_start_event_identity_is_stable(monkeypatch):
    states = Mock(start_execution=Mock(return_value={"executionArn": "execution"}))
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test")
    monkeypatch.setattr(start_run, "get_client", lambda svc: states if svc=='stepfunctions' else Mock(get_caller_identity=Mock(return_value={"Account":"123456789012"})))
    first = start_run.handler({"id":"event"}, None)
    second = start_run.handler({"id":"event"}, None)
    assert first['run_id'] == second['run_id'] and first['status'] == 'accepted'
    assert states.start_execution.call_args.kwargs['name'] == first['run_id']


def test_duplicate_execution_returns_existing_reference(monkeypatch):
    class Exists(Exception): pass
    states = Mock(exceptions=SimpleNamespace(ExecutionAlreadyExists=Exists),
                  describe_execution=Mock(return_value={'status':'RUNNING'}))
    states.start_execution.side_effect = Exists()
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test")
    monkeypatch.setattr(start_run, "get_client", lambda svc: states if svc=='stepfunctions' else Mock(get_caller_identity=Mock(return_value={"Account":"123456789012"})))
    result = start_run.handler({'id':'event'}, None)
    assert ':execution:test:' in result['execution_arn'] and result['status']=='accepted'


@pytest.mark.parametrize(('state','expected'), [('SUCCEEDED','completed'),('FAILED','failed'),('TIMED_OUT','failed'),('ABORTED','failed')])
def test_duplicate_execution_reports_terminal_state(monkeypatch, state, expected):
    class Exists(Exception): pass
    states = Mock(exceptions=SimpleNamespace(ExecutionAlreadyExists=Exists),
                  describe_execution=Mock(return_value={'status':state}))
    states.start_execution.side_effect = Exists()
    monkeypatch.setenv('STATE_MACHINE_ARN', 'arn:aws:states:us-east-1:123456789012:stateMachine:test')
    monkeypatch.setattr(start_run, 'get_client', lambda svc: states if svc=='stepfunctions' else Mock(get_caller_identity=Mock(return_value={'Account':'123456789012'})))
    result = start_run.handler({'id':'event'}, None)
    assert result['status']==expected
    if state in {'FAILED','TIMED_OUT','ABORTED'}:
        assert result['redrive_required'] is True


def test_redrive_key_creates_stable_new_execution_identity(monkeypatch):
    states = Mock(start_execution=Mock(return_value={'executionArn':'execution'}))
    monkeypatch.setenv('STATE_MACHINE_ARN', 'arn:aws:states:us-east-1:123456789012:stateMachine:test')
    monkeypatch.setattr(start_run, 'get_client', lambda svc: states if svc=='stepfunctions' else Mock(get_caller_identity=Mock(return_value={'Account':'123456789012'})))
    first = start_run.handler({'id':'event','redrive_key':'retry-1'}, None)
    payload = json.loads(states.start_execution.call_args.kwargs['input'])
    second = start_run.handler({'id':'event','redrive_key':'retry-1'}, None)
    assert first['run_id']==second['run_id']==payload['run_id']
    assert first['run_id'] != start_run.handler({'id':'event'}, None)['run_id']


def test_scanner_artifact_precedes_acknowledgment(monkeypatch):
    store = Mock(acquire=Mock(return_value=('lease', None)))
    monkeypatch.setattr(scanner_handler, 'RunStore', lambda: store)
    monkeypatch.setenv('REPORT_BUCKET', 'reports')
    scanned = {'scanner':'scan_unattached_eips','status':'complete','findings':[],'inventory':[],'errors':[]}
    monkeypatch.setattr(scanner_handler, 'run_scanner', Mock(return_value=scanned))
    archive = Mock()
    monkeypatch.setattr(scanner_handler, 'archive_json', archive)
    result = scanner_handler.handler(event(), SimpleNamespace(get_remaining_time_in_millis=lambda:270000))
    assert '/attempts/scan_unattached_eips/lease.json' in result['key']
    assert archive.call_args.args[0] == scanned
    store.finish.assert_called_once()


def test_cached_scanner_does_not_repeat_work(monkeypatch):
    monkeypatch.setattr(scanner_handler, 'RunStore', lambda: Mock(acquire=Mock(return_value=(None, {'key':'prior'}))))
    scanner = Mock()
    monkeypatch.setattr(scanner_handler, 'run_scanner', scanner)
    assert scanner_handler.handler(event(), None) == {'key':'prior'}
    scanner.assert_not_called()


def test_transient_scanner_errors_release_lease_and_retry(monkeypatch):
    store = Mock(acquire=Mock(return_value=('lease', None)))
    monkeypatch.setattr(scanner_handler, 'RunStore', lambda: store)
    monkeypatch.setenv('REPORT_BUCKET', 'reports')
    monkeypatch.setattr(scanner_handler, 'run_scanner', Mock(return_value={'scanner':'scan_unattached_eips','status':'failed','errors':[{'retryable':True}]}))
    monkeypatch.setattr(scanner_handler, 'archive_json', Mock())
    with pytest.raises(scanner_handler.RetryableScanError):
        scanner_handler.handler(event(), SimpleNamespace(get_remaining_time_in_millis=lambda:270000))
    store.release.assert_called_once()
    store.finish.assert_not_called()


def test_finalizer_reads_scoped_artifacts_and_catches_task_failure(monkeypatch):
    store = Mock(acquire=Mock(return_value=('lease', None)))
    monkeypatch.setattr(finalize_run, 'RunStore', lambda: store)
    monkeypatch.setenv('REPORT_BUCKET', 'reports')
    content = json.dumps({'scanner':'one','status':'complete','inventory':[],'findings':[],'errors':[]}).encode()
    s3 = Mock(head_object=Mock(return_value={'ContentLength':len(content)}),
              get_object=Mock(return_value={'Body':io.BytesIO(content)}))
    monkeypatch.setattr(finalize_run, 'get_client', lambda svc:s3)
    finish = Mock(return_value={'status':'partial'})
    monkeypatch.setattr(finalize_run, 'finalize', finish)
    ev = event()
    ev['outputs'] = [{'scanner':'one','bucket':'reports','key':'reports/123456789012/us-east-1/run/attempts/one/lease.json'}, {'scanner':'two','status':'failed'}]
    result = finalize_run.handler(ev, SimpleNamespace(get_remaining_time_in_millis=lambda:240000))
    assert result['status']=='partial' and finish.call_args.args[0][1]['status']=='failed'
    store.finish.assert_called_once()


def test_foreign_artifact_rejected(monkeypatch):
    monkeypatch.setattr(finalize_run, 'RunStore', lambda:Mock(acquire=Mock(return_value=('lease',None))))
    monkeypatch.setattr(finalize_run, 'get_client', Mock())
    monkeypatch.setenv('REPORT_BUCKET','reports')
    ev=event(); ev['outputs']=[{'scanner':'one','bucket':'other','key':'foreign'}]
    with pytest.raises(ValueError, match='outside'):
        finalize_run.handler(ev, None)


def test_finalizer_skips_oversized_artifact_without_reading_body(monkeypatch):
    store = Mock(acquire=Mock(return_value=('lease',None)))
    monkeypatch.setattr(finalize_run,'RunStore',lambda:store)
    monkeypatch.setenv('REPORT_BUCKET','reports')
    s3 = Mock(head_object=Mock(return_value={'ContentLength':9*1024*1024}))
    monkeypatch.setattr(finalize_run,'get_client',lambda _:s3)
    monkeypatch.setattr(finalize_run,'finalize',lambda results,*args,**kwargs:results)
    ev=event(); ev['outputs']=[{'scanner':'large','bucket':'reports','key':'reports/123456789012/us-east-1/run/attempts/large/attempt.json'}]
    output=finalize_run.handler(ev,SimpleNamespace(get_remaining_time_in_millis=lambda:240000))
    assert output[0]['status']=='failed' and output[0]['errors'][0]['type']=='ArtifactSizeLimit'
    s3.get_object.assert_not_called()


def test_conditional_run_store_cache_and_lease(monkeypatch):
    class Conflict(Exception): pass
    client=Mock(exceptions=SimpleNamespace(ConditionalCheckFailedException=Conflict))
    monkeypatch.setenv('RUN_TABLE','runs')
    store=RunStore(client)
    token, prior=store.acquire('run','stage')
    assert token and prior is None
    store.finish('run','stage',token,{'status':'complete'})
    assert client.update_item.call_args.kwargs['ConditionExpression']=='#token = :token'
    store.release('run','stage',token)
    assert client.update_item.call_args.kwargs['ConditionExpression']=='#token = :token AND #status <> :complete'
    client.put_item.side_effect=Conflict()
    client.get_item.return_value={'Item':{'status':{'S':'complete'},'result':{'S':'{"status":"complete"}'}}}
    assert store.acquire('run','stage')==(None,{'status':'complete'})
    client.get_item.return_value={'Item':{'status':{'S':'running'}}}
    with pytest.raises(RuntimeError, match='already running'):
        store.acquire('run','stage')


def test_artifact_build_allowlist_and_workflow_contract(tmp_path):
    from scripts.build_package import build
    root=build(tmp_path/'artifact',dependencies=False)
    assert (root/'lambda_handler.py').is_file()
    assert (root/'orchestration/finalize_run.py').is_file()
    assert not (root/'tests').exists() and not (root/'.git').exists()
    from pathlib import Path
    import yaml; template=yaml.safe_load(Path('infra/template.yaml').read_text(encoding='utf-8'))
    workflow=json.loads(Path('infra/workflow.asl.json').read_text())
    assert template['Resources']['Workflow']['Properties']['Type']=='STANDARD'
    assert workflow['States']['Scan']['MaxConcurrency']==4
    assert template['Resources']['ExecutionTimeoutAlarm']['Properties']['MetricName'] == 'ExecutionsTimedOut'
    assert any('RetryableStageError' in rule['ErrorEquals'] for rule in workflow['States']['Finalize']['Retry'])
    assert 'AWS_REGION' not in template['Resources']['ScannerFunction']['Properties']['Environment']['Variables']
    statements=template['Resources']['FinalizerRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
    assert next(s for s in statements if 'sns:Publish' in s.get('Action', []))['Resource']=={'Ref':'ReportTopic'}
    assert 'OperationsDashboard' in template['Resources']
    assert template['Parameters']['FinalizerInputBytes']['MaxValue']==2097152


def test_finalizer_read_failure_releases_lease_for_transient_retry(monkeypatch):
    from botocore.exceptions import ClientError
    from utils.run_store import RetryableStageError
    store = Mock(acquire=Mock(return_value=('lease', None)))
    monkeypatch.setattr(finalize_run, 'RunStore', lambda: store)
    monkeypatch.setenv('REPORT_BUCKET', 'reports')
    client = Mock()
    client.head_object.return_value = {'ContentLength': 1}
    client.get_object.side_effect = ClientError({'Error': {'Code': 'SlowDown'}}, 'GetObject')
    monkeypatch.setattr(finalize_run, 'get_client', lambda svc: client)
    ev = event()
    ev['outputs'] = [{'scanner': 'one', 'bucket': 'reports',
                      'key': 'reports/123456789012/us-east-1/run/attempts/one/lease.json'}]
    with pytest.raises(RetryableStageError):
        finalize_run.handler(ev, None)
    store.release.assert_called_once_with('run', 'finalize', 'lease')
    store.finish.assert_not_called()


def test_scanner_ack_failure_releases_lease(monkeypatch):
    store = Mock(acquire=Mock(return_value=('lease', None)))
    store.finish.side_effect = RuntimeError('ack failed')
    monkeypatch.setattr(scanner_handler, 'RunStore', lambda: store)
    monkeypatch.setenv('REPORT_BUCKET', 'reports')
    monkeypatch.setattr(scanner_handler, 'run_scanner', Mock(return_value={
        'scanner': 'scan_unattached_eips', 'status': 'complete', 'errors': []}))
    monkeypatch.setattr(scanner_handler, 'archive_json', Mock())
    with pytest.raises(RuntimeError, match='ack failed'):
        scanner_handler.handler(event(), SimpleNamespace(get_remaining_time_in_millis=lambda: 270000))
    store.release.assert_called_once_with('run', 'scan_unattached_eips', 'lease')


def test_release_failure_preserves_original_exception():
    from utils.run_store import release_on_failure
    store = Mock(release=Mock(side_effect=RuntimeError('release failed')))
    with pytest.raises(ValueError, match='original'):
        with release_on_failure(store, 'run', 'stage', 'lease'):
            raise ValueError('original')


def test_artifact_smoke_rejects_missing_dependencies(tmp_path):
    import subprocess
    from scripts.build_package import build
    from scripts.validate_artifact import validate
    root = build(tmp_path / 'artifact', dependencies=False)
    with pytest.raises(subprocess.CalledProcessError):
        validate(root)
