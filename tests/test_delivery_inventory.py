import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from utils.config import Config
from utils.scan_context import ScanContext, activate
from utils import aws_factory
from scanner.inventory import TrackedClient
from delivery import s3_archiver, sns_sender


def test_factory_uses_task_snapshot_and_reuses_client(monkeypatch):
    raw=Mock(meta=SimpleNamespace(region_name='eu-west-1'), describe_addresses=Mock(return_value={'Addresses':[]}))
    session=Mock(client=Mock(return_value=raw))
    monkeypatch.setattr(aws_factory.boto3.session,'Session',lambda:session)
    from dataclasses import replace
    scope=ScanContext('test',replace(Config.load(),region='eu-west-1'))
    with activate(scope):
        first=aws_factory.get_client('ec2')
        assert aws_factory.get_client('ec2') is first
        assert first.meta.region_name=='eu-west-1'
        first.describe_addresses()
    assert session.client.call_args.kwargs['region_name']=='eu-west-1'


def test_client_failure_records_incomplete_operation():
    raw=Mock(describe_addresses=Mock(side_effect=RuntimeError('denied')))
    scope=ScanContext('test',Config.load())
    with pytest.raises(RuntimeError):
        TrackedClient(raw,scope).describe_addresses()
    assert scope.operations['describe_addresses'] is False and scope.errors


def test_scan_operation_details_are_explicit_and_failed_calls_stay_incomplete():
    scope=ScanContext('test',Config.load())
    tracked=TrackedClient(Mock(describe_addresses=Mock(return_value={'Addresses':[]})),scope)
    tracked.describe_addresses()
    result=scope.result([],0)
    assert result['operations']=={'describe_addresses':True}
    assert result['operation_details']['describe_addresses']=={'required':True,'attempts':1,'status':'complete'}
    failed=ScanContext('test',Config.load())
    client=TrackedClient(Mock(describe_volumes=Mock(side_effect=RuntimeError('denied'))),failed)
    with pytest.raises(RuntimeError):
        client.describe_volumes()
    result=failed.result([],0)
    assert result['discovery_complete'] is False
    assert result['operation_details']['describe_volumes']['status']=='failed'


def test_inventory_covers_active_instances_and_tag_updates():
    scope=ScanContext('inventory',Config.load(),'123456789012')
    tracked=TrackedClient(Mock(),scope)
    with activate(scope):
        tracked.capture('describe_instances', {'Reservations':[{'Instances':[{'InstanceId':'active','State':{'Name':'running'}}]}]})
        tracked.capture('describe_load_balancers',{'LoadBalancers':[{'LoadBalancerName':'lb','LoadBalancerArn':'arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/lb/id'}]})
        tracked.capture('describe_tags',{'TagDescriptions':[{'ResourceArn':'arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/lb/id','Tags':[{'Key':'owner','Value':'sre'}]}]})
    assert len(scope.inventory)==2
    assert next(r for r in scope.inventory.values() if r['id']=='lb')['tags']=={'owner':'sre'}


def test_rds_inventory_reads_tags_for_active_nonfindings():
    raw=Mock(list_tags_for_resource=Mock(return_value={'TagList':[{'Key':'owner','Value':'dba'}]}))
    scope=ScanContext('rds',Config.load(),'123456789012')
    tracked=TrackedClient(raw,scope)
    with activate(scope):
        tracked.capture('describe_db_instances',{'DBInstances':[{'DBInstanceIdentifier':'active','DBInstanceArn':'arn:aws:rds:us-east-1:123456789012:db:active'}]})
    assert next(iter(scope.inventory.values()))['tags']=={'owner':'dba'}


def test_archives_use_explicit_keys_and_kms(monkeypatch):
    s3=Mock()
    monkeypatch.setattr(s3_archiver,'_s3_client',s3)
    monkeypatch.setenv('REPORT_BUCKET','reports')
    monkeypatch.setenv('S3_KMS_KEY_ID','arn:aws:kms:us-east-1:123456789012:key/key')
    monkeypatch.setenv('S3_STORAGE_CLASS','STANDARD_IA')
    assert s3_archiver.archive_report('full',key='reports/run/report.md')=='reports/run/report.md'
    assert s3.put_object.call_args.kwargs['ServerSideEncryption']=='aws:kms'
    s3_archiver.archive_json({'unknown':None},'reports/run/manifest.json')
    assert json.loads(s3.put_object.call_args.kwargs['Body'])=={'unknown':None}
    assert s3.put_object.call_args.kwargs['ContentType']=='application/json'


def test_s3_idempotent_put_verifies_existing_object(monkeypatch):
    from botocore.exceptions import ClientError
    s3 = Mock()
    monkeypatch.setattr(s3_archiver, '_s3_client', s3)
    monkeypatch.setenv('REPORT_BUCKET', 'reports')
    body = json.dumps({'run':'stable'}, allow_nan=False, default=str).encode()
    import hashlib
    digest = hashlib.sha256(body).hexdigest()
    s3.put_object.side_effect = ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
    s3.head_object.return_value = {'Metadata': {'waste-hunter-sha256': digest}}
    assert s3_archiver.archive_json({'run':'stable'}, 'reports/run/manifest.json') == 'reports/run/manifest.json'
    assert s3.put_object.call_args.kwargs['IfNoneMatch'] == '*'
    s3.head_object.assert_called_once()


def test_s3_idempotent_key_rejects_different_content(monkeypatch):
    from botocore.exceptions import ClientError
    s3 = Mock()
    monkeypatch.setattr(s3_archiver, '_s3_client', s3)
    monkeypatch.setenv('REPORT_BUCKET', 'reports')
    s3.put_object.side_effect = ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
    s3.head_object.return_value = {'Metadata': {'waste-hunter-sha256': 'different'}}
    with pytest.raises(ValueError, match='different content'):
        s3_archiver.archive_json({'run':'new'}, 'reports/run/manifest.json')


@pytest.mark.parametrize('bucket', ['arn:aws:s3:::bucket','bucket/path'])
def test_invalid_archive_bucket_rejected(monkeypatch,bucket):
    monkeypatch.setenv('REPORT_BUCKET',bucket)
    with pytest.raises(ValueError):
        s3_archiver.archive_report('report')


def test_archive_failure_is_not_acknowledged(monkeypatch):
    monkeypatch.setenv('REPORT_BUCKET','reports')
    monkeypatch.setattr(s3_archiver,'_s3_client',Mock(put_object=Mock(side_effect=RuntimeError('denied'))))
    with pytest.raises(RuntimeError):
        s3_archiver.archive_report('full')


@pytest.mark.parametrize('value', [None,'bad',float('nan'),float('inf'),10**10])
def test_invalid_sns_numeric_attributes_are_omitted(value):
    assert 'monthly_waste' not in sns_sender._build_message_attributes({'monthly_waste':value})


def test_sns_topic_region_and_subject(monkeypatch):
    monkeypatch.setenv('SNS_TOPIC_REGION','eu-west-1')
    assert sns_sender._topic_region()=='eu-west-1'
    monkeypatch.delenv('SNS_TOPIC_REGION')
    monkeypatch.setenv('SNS_TOPIC_ARN','arn:aws:sns:us-east-1:123456789012:reports')
    assert sns_sender._topic_region()=='us-east-1'
    monkeypatch.setenv('REPORT_SCHEDULE','Daily\nInjected')
    sns=Mock(publish=Mock(return_value={'MessageId':'id'}))
    monkeypatch.setattr(sns_sender,'_sns_client',sns)
    sns_sender.send_report('full', {'status':'ok','high_waste':False,'compliance_percent':12.5,'no_metrics':1})
    assert '\n' not in sns.publish.call_args.kwargs['Subject']


def test_sns_payload_has_stable_run_deduplication_key(monkeypatch):
    monkeypatch.setenv('SNS_TOPIC_ARN', 'arn:aws:sns:us-east-1:123456789012:reports')
    sns = Mock(publish=Mock(return_value={'MessageId':'id'}))
    monkeypatch.setattr(sns_sender, '_sns_client', sns)
    sns_sender.send_report('run run-123', {'run_id':'run-123','status':'ok'})
    assert sns.publish.call_args.kwargs['MessageAttributes']['run_id']['StringValue']=='run-123'


def test_sns_failure_is_visible_to_retry_caller(monkeypatch):
    monkeypatch.setenv('SNS_TOPIC_ARN', 'arn:aws:sns:us-east-1:123456789012:reports')
    sns = Mock(publish=Mock(side_effect=RuntimeError('publish failed')))
    monkeypatch.setattr(sns_sender, '_sns_client', sns)
    with pytest.raises(RuntimeError, match='publish failed'):
        sns_sender.send_report('report', {'run_id':'run-123','status':'ok'})


def test_configuration_and_price_snapshot_do_not_drift(monkeypatch):
    from cost_engine import estimator
    config=Config.load()
    monkeypatch.setenv('PRICING_JSON','{"EC2":{"t3.micro":999}}')
    with activate(ScanContext('pricing',config)):
        rows,total=estimator.estimate_monthly_waste([{'type':'EC2','id':'i','region':'us-east-1','instance_type':'t3.micro'}])
    assert total==8.5
