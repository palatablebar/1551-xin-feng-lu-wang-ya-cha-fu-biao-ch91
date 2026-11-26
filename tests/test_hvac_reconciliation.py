from dataclasses import replace
import pytest
from ductwatch.hvac_reconciliation.service import check_flow, compare, data_fingerprint, policy_fingerprint, sample_versions
from ductwatch.hvac_reconciliation.contracts import LedgerCheck
from ductwatch.pipeline import execute, open_fixture, preview


def test_payload_identity_remains_if_import_order_changes(observation):
    # Reordering the import preserves decisions, as checked separately by the API integration test.
    second = {**observation,'key':'linked'}
    assert data_fingerprint([observation,second]) == data_fingerprint([second,observation])
    assert data_fingerprint([observation]) != data_fingerprint([observation,second])


def test_policy_change_recomputes_audit_stamp():
    assert policy_fingerprint(4,'focus','fv','fit') != policy_fingerprint(4,'balanced','fv','fit')
    assert policy_fingerprint(4,'focus','fv','fit') != policy_fingerprint(4,'focus','fv2','fit')


def test_incomplete_balance_cannot_be_signed():
    with pytest.raises(ArithmeticError,match='额度'):
        LedgerCheck(10,0,0,5,9,8,2,(('派发',5),))


def test_missing_decision_cannot_be_signed():
    with pytest.raises(ArithmeticError,match='数量'):
        LedgerCheck(10,0,0,5,9,8,1,(('派发',4),))


def test_reference_revision_signature_tracks_changes(observation):
    later = {**observation,'revision':2}
    assert data_fingerprint([observation]) != data_fingerprint([later])


def test_sample_settlement_counts_match_worklist():
    report = preview()
    assert sum(count for _,count in report['flow_counts']) == len(report['rows'])
    assert sum(row['cost'] for row in report['rows'] if row['state'] == '已分派') + report['metrics'][-1]['value'] == 12


def test_repeat_results_are_identical():
    assert preview() == preview()


def test_late_correction_replaces_training_reading_but_keeps_old_voucher():
    dataset = open_fixture()
    entries = [dict(row) for row in dataset['records']]
    original = next(row for row in entries if row['key'] == 'AF-1101')
    correction = {**original, 'revision': 2, 'received': 94,
                  'pressure_pa': original['pressure_pa'] + 1}

    reissued = execute(entries, as_of=91)
    with_correction = execute(entries + [correction], as_of=91)
    # Re-signing the old shift at its original cutoff reproduces the first voucher.
    assert with_correction['run_id'] == reissued['run_id']
    assert with_correction['sample_hash'] == reissued['sample_hash']

    # The current shift now trains on the replacement reading, not the retracted one.
    current = execute(entries + [correction], as_of=96)
    plain = execute(entries, as_of=96)
    assert current['feature_stamp'] != plain['feature_stamp']
    adopted = {row['单号']: row for row in current['sample_versions']}
    assert adopted['AF-1101']['采用版次'] == 2
    assert adopted['AF-1101']['替代版次'] == 1 and adopted['AF-1101']['迟到更正']


def test_voucher_lists_the_adopted_revision():
    report = preview()
    versions = {item['单号']: item for item in report['sample_versions']}
    assert versions and all('采用版次' in item for item in versions.values())
    # Every ticket actually dispatched this shift shows a revision on the voucher.
    assert {row['key'] for row in report['rows']} <= versions.keys()
