# -*- coding: utf-8 -*-
"""Shared, side-effect-free calculation helpers.

All lookbacks are expressed in observations (normally trading days), not
calendar days.  Keeping these primitives in one module prevents the batch
report and the web dashboard from silently using different definitions.
"""
import numpy as np
import pandas as pd


def lookback_change_pct(values, periods):
    """Return latest value versus *periods* observations ago, in percent.

    If less history is available, use the earliest valid observation.  Zero
    and non-finite bases are rejected because their percentage change is not
    economically meaningful.
    """
    s = pd.Series(values, dtype="float64").dropna()
    if len(s) < 2:
        return np.nan
    steps = min(int(periods), len(s) - 1)
    base = s.iloc[-1 - steps]
    latest = s.iloc[-1]
    if not np.isfinite(base) or not np.isfinite(latest) or base == 0:
        return np.nan
    return (latest / base - 1) * 100
