"""Adaptive publication allowance; discovery stock may contain more agents."""

REALTOR_BASE_PERCENT = 15
REALTOR_MAX_PERCENT = 20


def realtor_allowance(ordinary_count: int, target: int, *, base_allowance: int | None = None) -> int:
    """Use 15% with adequate supply, up to 20% to fill a shortage.

    Cap against actual ordinary supply too, so a short feed cannot exceed 20%.
    """
    ordinary_count, target = max(0, ordinary_count), max(0, target)
    baseline = target * REALTOR_BASE_PERCENT // 100 if base_allowance is None else max(0, base_allowance)
    return min(max(baseline, target - ordinary_count), target * REALTOR_MAX_PERCENT // 100,
               ordinary_count * REALTOR_MAX_PERCENT // (100 - REALTOR_MAX_PERCENT))
