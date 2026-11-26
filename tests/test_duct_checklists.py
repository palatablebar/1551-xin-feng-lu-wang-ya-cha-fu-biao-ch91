from dataclasses import replace
import pytest
from ductwatch.duct_checklists.service import load_samples, number, profile_intake, read_assay


def test_normalized_number_field(observation):
    value = observation['pressure_pa']
    source = {**observation, 'pressure_pa': str(value), 'key': '　ＡＢ-１２　'}
    row = read_assay(source)
    assert row.key == 'AB-12'
    assert row.pressure_pa == value


def test_bool_and_nonfinite_values_are_isolated(observation):
    tickets = [{**observation, 'pressure_pa': raw} for raw in [True, 'Infinity', 'NaN', [], 'x'*100]]
    result = load_samples(tickets + [observation], 96)
    assert len(result.entries) == 1
    assert [item.code for item in result.issues] == ['NUMBER_REQUIRED','NUMBER_RANGE','NUMBER_RANGE','NUMBER_REQUIRED','NUMBER_INVALID']


def test_latest_recognized_version_before_cutoff(observation):
    result = load_samples([observation, {**observation,'revision':2}, {**observation,'revision':3,'received':100}], 96)
    assert [entry.revision for entry in result.entries] == [2]
    assert [issue.code for issue in result.issues] == ['OLDER_REVISION','AFTER_CUTOFF']


def test_conflicting_same_version_is_order_independent(observation):
    # TRAP-14: no arrival-order overwrite.
    alternative = {**observation, 'pressure_pa': observation['pressure_pa'] + .1}
    for documents in ([observation, alternative], [alternative, observation]):
        loaded = load_samples(documents, 96)
        assert loaded.entries == ()
        assert [row.code for row in loaded.issues] == ['CONFLICTING_REVISION'] * 2


def test_import_report_conserves_rejected_rows(observation):
    profile = profile_intake(load_samples([observation, dict(observation), None], 96))
    assert profile['received'] == profile['retained'] + profile['discarded'] == 3
    assert profile['by_issue'] == {'OBJECT_REQUIRED':1,'REPEATED_REVISION':1}


def test_result_requires_mature_timestamp_pair(observation):
    document = {**observation,'result_day':None}
    report = load_samples([document], 96)
    assert report.issues[0].code == 'RESULT_TIMESTAMP_PAIR_REQUIRED'


def test_numeric_lower_bound_is_inclusive():
    assert number('4','quantity',4,10,integral=True) == 4
    with pytest.raises(ValueError, match='WHOLE_REQUIRED'):
        number('4.1','quantity',4,10,integral=True)


def test_confirmed_correction_links_to_its_predecessor(observation):
    # A later revision that arrives after the shift cutoff still supersedes the
    # earlier reading, so the replacement can be routed to the fold it corrects.
    correction = {**observation, 'revision': 2, 'received': 94,
                  'pressure_pa': observation['pressure_pa'] + 1}
    report = load_samples([observation, correction], 96)
    kept = report.entries[0]
    assert (kept.revision, kept.amends.revision) == (2, 1)
    assert [issue.code for issue in report.issues] == ['OLDER_REVISION']


def test_same_version_conflict_drops_the_correction_without_fallback(observation):
    # Same-version corrections disagree: the whole sample goes, not the wrong reading.
    low = {**observation, 'revision': 2, 'received': 94, 'pressure_pa': observation['pressure_pa'] + 1}
    high = {**observation, 'revision': 2, 'received': 94, 'pressure_pa': observation['pressure_pa'] + 2}
    report = load_samples([observation, low, high], 96)
    assert report.entries == ()
    assert [issue.code for issue in report.issues] == ['CONFLICTING_REVISION'] * 3


def test_identical_repeat_of_a_correction_stays_linked(observation):
    correction = {**observation, 'revision': 2, 'received': 94,
                  'pressure_pa': observation['pressure_pa'] + 1}
    report = load_samples([observation, correction, dict(correction)], 96)
    assert [(row.revision, row.amends.revision) for row in report.entries] == [(2, 1)]
    assert [issue.code for issue in report.issues] == ['OLDER_REVISION', 'REPEATED_REVISION']


def test_stacked_corrections_anchor_to_the_original(observation):
    base = {**observation, 'day': 10, 'received': 11, 'result_day': 14, 'revision': 1}
    second = {**base, 'revision': 2, 'received': 40, 'pressure_pa': base['pressure_pa'] + 1}
    third = {**base, 'revision': 3, 'received': 94, 'pressure_pa': base['pressure_pa'] + 2}
    for order in ([base, second, third], [third, base, second], [second, third, base]):
        report = load_samples(order, 96)
        assert [(row.revision, row.amends.revision) for row in report.entries] == [(3, 1)]
