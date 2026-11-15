"""Adaptive combined allowance for realtors and unclassified authors."""

REALTOR_BASE_PERCENT = 10
REALTOR_MAX_PERCENT = 15


def realtor_allowance(ordinary_count: int, target: int, *, base_allowance: int | None = None) -> int:
    """Use 10% with sufficient owners, rising to 15% when stock is short."""
    del base_allowance
    owners, target = max(0, ordinary_count), max(0, target)
    if not target:
        return 0
    percent = REALTOR_BASE_PERCENT if owners * 100 >= target * 90 else REALTOR_MAX_PERCENT
    return min(target * percent // 100, owners * percent // (100 - percent))
