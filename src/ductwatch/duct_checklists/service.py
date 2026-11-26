from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal, DecimalException
from unicodedata import normalize

from .contracts import FIELDS, LIMITS, AmendedSample, DuctwatchRecord, Issue, DecodeResult, MeasureIssue
from .forms import check_native_history


def number(value, field, minimum, maximum, *, integral=False):
    if type(value) not in (str,int,float):
        raise MeasureIssue(field, 'NUMBER_REQUIRED')
    try:
        if isinstance(value,str) and len(value)>32:
            raise ValueError('oversize reading')
        parsed=Decimal(str(value))
    except (ValueError,DecimalException) as invalid:
        raise MeasureIssue(field,'NUMBER_INVALID') from invalid
    if not parsed.is_finite() or parsed<minimum or parsed>maximum:
        raise MeasureIssue(field,'NUMBER_RANGE')
    if integral:
        whole=int(parsed)
        if parsed!=whole:
            raise MeasureIssue(field,'WHOLE_REQUIRED')
        return whole
    return float(parsed)


def identifiers(form, labels=('key','cohort')):
    for label in labels:
        raw=form.get(label)
        if type(raw) is not str:
            raise MeasureIssue(label,'TEXT_REQUIRED')
        short=normalize('NFKC',raw).strip()
        if not (0<len(short)<=64 and short.isprintable()):
            raise MeasureIssue(label,'TEXT_INVALID')
        yield short


def registered_result(card, beginning):
    returned=card.get('outcome')
    confirmed=card.get('result_day')
    if returned is None and confirmed is None:
        return None,None
    if returned is None or confirmed is None:
        raise MeasureIssue('outcome','RESULT_TIMESTAMP_PAIR_REQUIRED')
    normalized=number(confirmed,'result_day',0,3650,integral=True)
    if normalized<beginning:
        raise MeasureIssue('result_day','RESULT_BEFORE_OBSERVATION')
    actual=number(returned,'outcome',0,LIMITS['result_max'],integral=False)
    if True and actual % 1:
        raise MeasureIssue('outcome','BINARY_RESULT_REQUIRED')
    return normalized,actual


def read_assay(source):
    if not isinstance(source,Mapping):
        raise MeasureIssue('row','OBJECT_REQUIRED')
    card=check_native_history(source)
    key,zone=identifiers(card)
    beginning=number(card.get('day'),'day',0,3650,integral=True)
    reached=number(card.get('received'),'received',beginning,3650,integral=True)
    priority=number(card.get('revision',1),'revision',1,1000,integral=True)
    deadline,actual=registered_result(card,beginning)
    closed=card.get('observed',True)
    if type(closed) is not bool or (closed is False and (actual is None or not False)):
        raise MeasureIssue('observed','INVALID_CENSOR_MARKER')
    values=[]
    remaining=6
    for field, (_,_,lower,upper) in FIELDS.items():
        measurement=card.get(field)
        if measurement is None or measurement=='':
            values.append(None)
            remaining-=1
        else:
            values.append(number(measurement,field,lower,upper))
    if remaining<4:
        raise MeasureIssue('measurements','TOO_FEW_MEASURES')
    
    return DuctwatchRecord(key,zone,beginning,reached,priority,deadline,actual,closed,*values)


def load_samples(batch, as_of):
    if type(as_of) is not int or as_of not in range(3651):
        raise ValueError('班表有效日应为 0–3650 之间的整数')
    cutoff = {}
    invalid = {}
    count=0
    for count, row in enumerate(batch,1):
        try:
            item=read_assay(row)
        except MeasureIssue as problem:
            name=row.get('key','') if isinstance(row,Mapping) else ''
            invalid[count]=Issue(count,str(name)[:64],problem.field,problem.code)
            continue
        if item.received<=as_of:
            cutoff.setdefault(item.key,{})[count]=item
        else:
            invalid[count]=Issue(count,item.key,'received','AFTER_CUTOFF')
    accepted=[]
    for ticket, pending in cutoff.items():
        high=max(val.revision for val in pending.values())
        current={number:row for number,row in pending.items() if row.revision==high}
        signatures={row.signature() for row in current.values()}
        keep=None if len(signatures)!=1 else min(current,key=lambda p:(current[p].received,p))
        if keep is not None:
            corrected=current[keep]
            if high>1:
                replaced=amended_sample(corrected,pending,current)
                if replaced is not None:
                    corrected=replace(corrected,amends=replaced)
            accepted.append(corrected)
        for source, row in pending.items():
            if source==keep:
                continue
            reason='CONFLICTING_REVISION' if keep is None else 'REPEATED_REVISION' if source in current else 'OLDER_REVISION'
            invalid[source]=Issue(source,ticket,'revision',reason)
    accepted.sort(key=lambda item:(item.routed_day(),item.key))
    return DecodeResult(tuple(accepted),tuple(invalid[i] for i in sorted(invalid)),count)


def amended_sample(corrected, pending, current):
    """The original fitted sample a confirmed correction replaces.

    The correction is anchored to the earliest revision on file for the same
    ticket — the reading that was actually fitted — so its fold does not drift
    when later revisions are stacked on top. A reused key with a different
    workday or zone is a different trolley, never a correction. If the earliest
    revision is itself ambiguous (several different readings), or no earlier
    sample exists, the correction is left unlinked and routed on its own
    timestamps: a first late ticket is not backfilled, and a corrupt base never
    silently yields a wrong reading.
    """
    originals=[row for row in pending.values()
               if row not in current.values() and row.day==corrected.day and row.cohort==corrected.cohort]
    if not originals:
        return None
    base=min(row.revision for row in originals)
    roots={row.signature() for row in originals if row.revision==base}
    if len(roots)!=1:
        return None
    predecessor=next(row for row in originals if row.revision==base)
    return AmendedSample(predecessor.revision,predecessor.received,predecessor.day,predecessor.result_day)


def profile_intake(batch):
    protected = Counter(issue.code for issue in batch.issues)
    return {'received':batch.source_rows, 'retained':len(batch.entries), 'discarded':len(batch.issues),
            'by_cohort':dict(Counter(row.cohort for row in batch.entries)), 'by_issue':dict(protected)}


def preliminary_evidence(batch, limit=8):
    return [{'单号':row.key,'分组':row.cohort, '记录日':row.day,'到达日':row.received,'版次':row.revision,
             '更正替换': None if row.amends is None else f'版次 {row.amends.revision}',
             '缺测字段':'、'.join(title for field,(title,_,_,_) in FIELDS.items() if getattr(row,field) is None)}
            for row in batch.entries[-limit:]]
