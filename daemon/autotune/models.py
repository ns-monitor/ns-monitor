"""
models.py -- Data structures and constants for the Autotune engine.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import copy

MMOL_TO_MGDL = 18.0182

@dataclass
class Profile:
    name: str
    dia: float                      # Hours (e.g. 5.0)
    peak: float                     # Minutes (e.g. 55.0)
    basal: List[float]              # 24 hourly rates U/h (index 0 = 00:00..01:00)
    isf_mgdl: float                 # mg/dL/U
    carb_ratio: float               # g/U (Carb ratio / IC)
    target_low_mgdl: float = 100.0  # mg/dL
    target_high_mgdl: float = 100.0 # mg/dL
    timezone_name: str = "UTC"

    @property
    def isf_mmol(self) -> float:
        """ISF in mmol/L per Unit."""
        return round(self.isf_mgdl / MMOL_TO_MGDL, 2)

    @property
    def csf_mgdl(self) -> float:
        """Carbohydrate Sensitivity Factor: ISF / CR (mg/dL per gram)."""
        if self.carb_ratio > 0:
            return round(self.isf_mgdl / self.carb_ratio, 2)
        return 0.0

    @property
    def csf_mmol(self) -> float:
        """Carbohydrate Sensitivity Factor in mmol/L per gram."""
        return round(self.csf_mgdl / MMOL_TO_MGDL, 3)

    @property
    def total_daily_basal(self) -> float:
        """Sum of 24 hourly basal rates (U/day)."""
        return round(sum(self.basal), 3)

    def copy(self) -> 'Profile':
        return copy.deepcopy(self)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "dia": self.dia,
            "peak": self.peak,
            "basal": [round(b, 3) for b in self.basal],
            "total_daily_basal": self.total_daily_basal,
            "isf_mgdl": round(self.isf_mgdl, 1),
            "isf_mmol": self.isf_mmol,
            "carb_ratio": round(self.carb_ratio, 2),
            "csf_mgdl": self.csf_mgdl,
            "csf_mmol": self.csf_mmol,
            "target_low_mgdl": round(self.target_low_mgdl, 1),
            "target_high_mgdl": round(self.target_high_mgdl, 1),
            "target_low_mmol": round(self.target_low_mgdl / MMOL_TO_MGDL, 1),
            "target_high_mmol": round(self.target_high_mgdl / MMOL_TO_MGDL, 1),
            "timezone": self.timezone_name
        }


@dataclass
class GlucosePoint:
    ts_ms: int
    sgv: float                      # mg/dL
    direction: str = ""

    @property
    def sgv_mmol(self) -> float:
        return round(self.sgv / MMOL_TO_MGDL, 1)


@dataclass
class Dose:
    ts_ms: int
    amount: float                   # Units of insulin
    dose_type: str                  # 'bolus', 'smb', 'pseudo_bolus'
    duration_min: float = 0.0


@dataclass
class TempBasalSegment:
    ts_ms: int
    duration_min: float
    rate: float                     # Actual delivered rate (U/h)


@dataclass
class PreparedPoint:
    ts_ms: int
    sgv: float                      # mg/dL
    avg_delta: float                # mg/dL per 5 min
    bgi: float                      # mg/dL per 5 min
    deviation: float                # mg/dL per 5 min
    cob: float                      # grams
    iob: float                      # Units
    activity: float                 # U / min
    classification: str             # 'CSF_MEAL', 'UAM', 'BASAL', 'ISF'

    @property
    def sgv_mmol(self) -> float:
        return round(self.sgv / MMOL_TO_MGDL, 1)

    @property
    def deviation_mmol(self) -> float:
        return round(self.deviation / MMOL_TO_MGDL, 2)


@dataclass
class DayClassificationStats:
    date_str: str
    total_points: int
    cgm_coverage_pct: float
    meal_points: int
    uam_points: int
    basal_points: int
    isf_points: int
    excluded_points: int
    status: str
    tuned_basal_total: float
    tuned_isf_mmol: float
    tuned_cr: float


@dataclass
class HourlyBasalComparison:
    hour: int
    pump_rate: float
    tuned_rate: float
    delta_rate: float
    pct_change: float
    min_cap: float
    max_cap: float
    capped: bool
    deviation_sum_mgdl: float
    deviation_sum_mmol: float
