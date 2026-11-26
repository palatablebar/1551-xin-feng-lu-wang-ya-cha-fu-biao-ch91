from dataclasses import replace
import pytest
from ductwatch.pressure_epochs.contracts import Span, WindowRule
from ductwatch.pressure_epochs.service import assign_fold, build_windows, model_inputs, partition_summary
from ductwatch.duct_checklists.service import read_assay, load_samples


def test_half_open_days():
    # TRAP-23: closed-open bounds exclude the first embargo day.
    span = Span(4, 12)
    assert 4 in span and 11 in span and 12 not in span


def test_mature_label_cannot_cross_partition(observation):
    # TRAP-42
    row = read_assay({**observation,'day':37,'received':38,'result_day':40})
    assert assign_fold(row, WindowRule.standard()) == (None, 'RESULT_NOT_MATURE')


def test_late_correction_routes_to_the_fold_it_corrects(observation):
    original = {**observation, 'day': 10, 'received': 11, 'result_day': 14}
    correction = {**original, 'revision': 2, 'received': 94,
                  'pressure_pa': observation['pressure_pa'] + 1}
    windows = build_windows(load_samples([original, correction], 96).entries)
    kept = next(row for row in windows.train if row.key == observation['key'])
    assert (kept.revision, kept.pressure_pa) == (2, observation['pressure_pa'] + 1)
    assert not any(row.key == observation['key'] for row in windows.live)


def test_late_first_ticket_is_not_backfilled(observation):
    late = read_assay({**observation, 'key': 'fresh-late', 'day': 10,
                       'received': 94, 'result_day': 14})
    windows = build_windows((late,))
    assert windows.train == ()
    assert windows.excluded == (('fresh-late', 'RESULT_NOT_MATURE'),)


def test_live_result_is_never_training(observation):
    row = read_assay({**observation,'day':90,'received':90,'result_day':90})
    assert assign_fold(row, WindowRule.standard()) == ('live','OPEN_RUN')


def test_fit_window_is_unchanged_by_future_labels(observation):
    row = read_assay(observation)
    future = replace(row,key='future',day=90,received=90,result_day=92,outcome=0.)
    assert build_windows((row,future)).train == build_windows((row,)).train


def test_overlap_configuration_rejected():
    with pytest.raises(ValueError, match='分窗重叠'):
        replace(WindowRule.standard(), calibration=Span(37,62))


def test_window_profile_accounts_for_embargo(observation):
    row = read_assay(observation)
    embargo = replace(row,key='break',day=41,received=42,result_day=43)
    windows = build_windows((row,embargo))
    assert [item.count for item in partition_summary(windows)] == [1,0,0,0]
    assert windows.excluded == (('break','EMBARGO_OR_UNSCHEDULED'),)


def test_underpopulated_train_rejected(observation):
    with pytest.raises(ValueError,match='16'):
        model_inputs(build_windows((read_assay(observation),)))
