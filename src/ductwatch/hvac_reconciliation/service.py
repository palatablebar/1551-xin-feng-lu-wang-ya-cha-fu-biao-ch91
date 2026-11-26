from .work_order import check_hvac_reconciliation as site_checklist
from collections import Counter, defaultdict
from dataclasses import asdict
from hashlib import blake2s, sha256
from json import dumps

from ..ventilation_factors.contracts import RAW_KEYS
from .control import accountable_plan, document_control
from .contracts import BatchReceipt, LedgerCheck, POLICY_VERSION


def emit_bytes(value):
    return dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()


def settled_record(record):
    """A stable, field-ordered view of one adopted sample for hashing.

    Timestamps are the routed ones, so a corrected sample keeps the day and
    arrival identity of the reading it replaces while carrying its new revision
    and measurements. Raw import dictionaries (standalone fingerprint checks)
    are hashed as they stand.
    """
    if isinstance(record, dict):
        return record
    return {'单号': record.key, '作业区': record.cohort, '工日': record.routed_day(),
            '版次': record.revision, '到站': record.received, '结果日': record.routed_result_day(),
            '结果': record.outcome, '观测': record.observed,
            '量测': {name: getattr(record, name) for name in RAW_KEYS}}


def data_fingerprint(records):
    per_record = Counter(sha256(emit_bytes(settled_record(row))).hexdigest() for row in records)
    return sha256(emit_bytes(sorted(per_record.items()))).hexdigest()


def intake_fingerprint(entries):
    """Fingerprint the readings the shift adopts, independent of import order.

    A correction deferred past the cutoff is not among the adopted entries, so
    the fingerprint stays on the original reading and the voucher reproduces.
    """
    return data_fingerprint(entries)


def policy_fingerprint(capacity, mode, feature_id, model_kind):
    contract = {'rule':POLICY_VERSION,'strategy':mode,'quantity':capacity,'features':feature_id,'model':model_kind}
    return blake2s(emit_bytes(contract),digest_size=8).hexdigest()


def scoped_counts(intake):
    """Counts that belong to this shift.

    Documents arriving after the shift cutoff are deferred to a later shift; they
    are still listed at intake for audit, but they cannot move a voucher already
    signed at that cutoff.
    """
    deferred = sum(1 for issue in intake.issues if issue.code == 'AFTER_CUTOFF')
    raw = intake.source_rows - deferred
    isolated = len(intake.issues) - deferred
    return raw, isolated, deferred


def check_flow(intake, windows, dispatched):
    expected = Counter(note.key for note in windows.live)
    outcome = Counter(entry.key for entry in dispatched.jobs)
    if expected != outcome:
        raise ArithmeticError('当班单据和执行单不一致')
    actual_used = defaultdict(int)
    phases = Counter()
    for item in dispatched.jobs:
        phases[item.state] += 1
        if item.state=='已分派':
            actual_used[item.cohort] += item.cost
    if actual_used != dict(dispatched.used_by_group):
        raise ArithmeticError('各区记录的任务工位数不一致')
    spend = sum(actual_used.values())
    if sum(len(rows) for _,rows in windows.partitions()) + len(windows.excluded) != len(intake.entries):
        raise ArithmeticError('成熟窗口记录和进站单据数不同')
    raw, isolated, _ = scoped_counts(intake)
    if len(intake.entries) + isolated != raw:
        raise ArithmeticError('进站隔离行缺失')
    return LedgerCheck(raw,isolated,len(windows.excluded),len(expected),
                       dispatched.capacity,spend,dispatched.balance,tuple(sorted(phases.items())))


def close_batch(intake,windows,dispatched,feature_id,model_kind,mode):
    summarized=check_flow(intake,windows,dispatched)
    origin=intake_fingerprint(intake.entries)
    policy=policy_fingerprint(dispatched.capacity,mode,feature_id,model_kind)
    entries=tuple((row.key,row.state,row.cost,row.reason,row.estimate) for row in dispatched.jobs)
    with_rows=tuple(sha256(emit_bytes(row)).hexdigest() for row in entries)
    stamp=blake2s(emit_bytes((origin,policy,with_rows,asdict(summarized))),digest_size=8).hexdigest()
    return BatchReceipt(stamp,origin,policy,summarized,entries)


def compare(before, after):
    old = {row[0]: row[1:] for row in before.decisions}
    current = {row[0]: row[1:] for row in after.decisions}
    new = current.keys() - old.keys()
    obsolete = old.keys() - current.keys()
    changed = {key for key in old.keys() & current.keys() if old[key] != current[key]}
    return {'新增': tuple(sorted(new)), '移出': tuple(sorted(obsolete)), '变更': tuple(sorted(changed)),
            '策略变化': before.policy_hash != after.policy_hash, '样本变化': before.batch_hash != after.batch_hash}


def reconciliation_evidence(receipt):
    ledger = receipt.totals
    rows = [{'核对项': '原始记录', '数量': ledger.raw}, {'核对项': '入站隔离', '数量': ledger.isolated},
            {'核对项': '窗口隔离', '数量': ledger.mature_excluded}, {'核对项': '当班对象', '数量': ledger.scored},
            {'核对项': '已用额度', '数量': ledger.resource_used}, {'核对项': '额度结余', '数量': ledger.balance}]
    rows.extend({'核对项': state, '数量': count} for state, count in ledger.states)
    return rows


def deferred_evidence(intake):
    """Rows arriving after the shift cutoff: listed, but never signed into this voucher."""
    deferred = [issue.evidence() for issue in intake.issues if issue.code == 'AFTER_CUTOFF']
    if not deferred:
        return []
    return [{'核对项': '逾期到站延后至下班', '数量': len(deferred)}, *deferred]


def sign_field_evidence(rows, allocation):
    counts, spent = accountable_plan(rows, allocation.balance, allocation.capacity)
    if dict(counts) != dict(Counter(job.state for job in allocation.jobs)):
        raise ArithmeticError('展示单据和签发方案的处置统计不符')
    selected = {row['key']: row['cost'] for row in rows if row['state'] == '已分派'}
    original = {job.key: job.cost for job in allocation.jobs if job.state == '已分派'}
    if selected != original:
        raise ArithmeticError('分派凭证与作业单互相矛盾')
    return {'工位使用':spent, '执行业务单':document_control(rows)}


def sample_versions(records):
    """Which revision of each ticket the shift actually settled on.

    Shown on the voucher so a reader can confirm a corrected ticket was signed on
    the replacement reading rather than the retracted one.
    """
    rows = []
    for record in records:
        amended = record.amends
        rows.append({'单号': record.key, '作业区': record.cohort,
                     '采用版次': record.revision,
                     '替代版次': None if amended is None else amended.revision,
                     '迟到更正': amended is not None})
    return rows


def intervention_summary(rows):
    actions = {}
    for record in rows:
        for sample in record['alternatives']:
            label = sample['情形']
            observed = actions.setdefault(label,[0,0,0,0])
            observed[0] += 1
            observed[1] += bool(sample['超训练域'])
            observed[2] += sample['用量']
            observed[3] += sample['预测变化']
    return [{'单项安排': key, '可比较单量': attempts, '超出拟合域': outlier,
             '总用量': units, '平均预测变化': round(delta/attempts,4)}
            for key,(attempts,outlier,units,delta) in sorted(actions.items())]
