from collections.abc import Mapping

import pandas as pd


def highest_absolute_correlation(
    candidate_returns: pd.Series,
    peer_returns: Mapping[str, pd.Series],
    *,
    window: int,
    minimum_observations: int,
) -> tuple[str, float, int] | None:
    if window < 2 or minimum_observations < 2 or minimum_observations > window:
        raise ValueError("Correlation window and minimum observations are invalid")
    candidate = candidate_returns.dropna().tail(window)
    if candidate.empty:
        return None

    highest = None
    for symbol, returns in peer_returns.items():
        aligned = pd.concat(
            [candidate.rename("candidate"), returns.dropna().rename("peer")],
            axis=1,
            join="inner",
        ).dropna().tail(window)
        if len(aligned) < minimum_observations:
            continue
        correlation = aligned["candidate"].corr(aligned["peer"])
        if pd.isna(correlation):
            continue
        absolute_correlation = abs(float(correlation))
        if highest is None or absolute_correlation > highest[1]:
            highest = (symbol, absolute_correlation, len(aligned))
    return highest


def validate_correlation_config(cfg) -> None:
    if (
        isinstance(cfg.correlation_lookback_sessions, bool)
        or not isinstance(cfg.correlation_lookback_sessions, int)
        or cfg.correlation_lookback_sessions < 2
    ):
        raise ValueError("Correlation lookback sessions must be an integer of at least 2")
    if (
        isinstance(cfg.correlation_min_observations, bool)
        or not isinstance(cfg.correlation_min_observations, int)
        or not 2 <= cfg.correlation_min_observations <= cfg.correlation_lookback_sessions
    ):
        raise ValueError(
            "Correlation minimum observations must be between 2 and the lookback sessions"
        )
    if (
        isinstance(cfg.correlation_absolute_threshold, bool)
        or not isinstance(cfg.correlation_absolute_threshold, (int, float))
        or not 0 < cfg.correlation_absolute_threshold <= 1
    ):
        raise ValueError("Absolute correlation threshold must be greater than 0 and at most 1")
