from dataclasses import dataclass

FIELDS = {'pressure_pa': ('滤网压差', 'Pa', 14, 340),
 'fan_pct': ('风机档位', '%', 18, 95),
 'flow_cmh': ('供风量', 'm³/h', 180, 3600),
 'pm_index': ('外部颗粒计数', '个/L', 3, 250),
 'open_pct': ('回风阀开度', '%', 5, 95),
 'since_days': ('换网后使用', '天', 3, 185)}
LIMITS = {'result_max': 1}


class MeasureIssue(ValueError):
    def __init__(self, field: str, code: str):
        super().__init__(f'{field}:{code}')
        self.field = field
        self.code = code


@dataclass(frozen=True, slots=True)
class AmendedSample:
    """The earlier revision a confirmed correction supersedes."""

    revision: int
    received: int
    day: int
    result_day: int | None


@dataclass(frozen=True, slots=True)
class DuctwatchRecord:
    key: str
    cohort: str
    day: int
    received: int
    revision: int
    result_day: int | None
    outcome: float | None
    observed: bool
    pressure_pa: float | None
    fan_pct: float | None
    flow_cmh: float | None
    pm_index: float | None
    open_pct: float | None
    since_days: float | None
    amends: AmendedSample | None = None

    def signature(self):
        # `amends` is intake bookkeeping, not a measurement, so two readings of the
        # same revision stay comparable only on the measured content.
        return (self.key, self.cohort, self.day, self.revision, self.result_day, self.outcome, self.observed,
                *(getattr(self, key) for key in FIELDS))

    def routed_day(self):
        if self.amends is None:
            return self.day
        return self.amends.day

    def routed_received(self):
        # A confirmed correction keeps the arrival time of the sample it replaces,
        # so it lands in the fold that sample was fitted in.
        if self.amends is None:
            return self.received
        return self.amends.received

    def routed_result_day(self):
        # The label event of the superseded sample does not move when only the
        # reading is corrected; a correction without a matured predecessor is
        # routed on its own timestamps (and stays a late, non-backfilled ticket).
        if self.amends is None:
            return self.result_day
        return self.amends.result_day


@dataclass(frozen=True, slots=True)
class Issue:
    source_row: int
    key: str
    field: str
    code: str

    def evidence(self):
        return {'行号': self.source_row, '单号': self.key, '字段': FIELDS.get(self.field, (self.field,))[0],
                '原因': self.code}


@dataclass(frozen=True, slots=True)
class DecodeResult:
    entries: tuple[DuctwatchRecord, ...]
    issues: tuple[Issue, ...]
    source_rows: int
