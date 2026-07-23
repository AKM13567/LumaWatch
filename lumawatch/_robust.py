"""
lumawatch._robust — shared robust-regression helper. Not a public module on
its own; used by lumawatch.calibration and lumawatch.schedule_learn.
"""

import statistics


def theil_sen(xs, ys, max_n=200):
    """Robust (slope, intercept) via Theil-Sen: the median of all pairwise
    slopes, and the median of (y - slope*x) as the matching intercept.
    Returns None if no valid pair exists (e.g. every x is identical).

    If there are more than max_n points, uses only the most recent max_n
    for this O(n^2) step -- xs/ys are assumed chronologically ordered, so
    "most recent" is just the tail slice. Keeps a single fit fast even
    after months of accumulated data.
    """
    if len(xs) > max_n:
        xs = xs[-max_n:]
        ys = ys[-max_n:]

    n = len(xs)
    pairwise_slopes = []
    for i in range(n):
        xi, yi = xs[i], ys[i]
        for j in range(i + 1, n):
            xj, yj = xs[j], ys[j]
            dx = xj - xi
            if dx == 0:
                continue
            pairwise_slopes.append((yj - yi) / dx)

    if not pairwise_slopes:
        return None

    slope = statistics.median(pairwise_slopes)
    intercept = statistics.median(y - slope * x for x, y in zip(xs, ys))
    return slope, intercept
