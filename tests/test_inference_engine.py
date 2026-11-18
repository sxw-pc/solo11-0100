"""Exact inference engine tests.

The row-space criterion (RREF unit row <=> coordinate uniquely determined) is
cross-checked against an independent brute-force integer solver that
enumerates the bounded integer solution space, on random 0/1 equation
systems. Concrete subtraction and simultaneous-equation cases are pinned too.
"""
import itertools
import random

from dockscan.privacy import fixed_boxes, rref, nullspace_basis
from fractions import Fraction


def brute_force_fixed(coefficients, totals, n, bound=12):
    """Independent check: enumerate integer assignments, return fixed coords."""
    fixed = set(range(n))
    seen_values = {i: set() for i in range(n)}
    for assignment in itertools.product(range(bound + 1), repeat=n):
        if all(sum(assignment[j] for j, v in enumerate(row) if v) == total
               for row, total in zip(coefficients, totals)):
            for j, value in enumerate(assignment):
                seen_values[j].add(value)
    for j in range(n):
        if len(seen_values[j]) != 1:
            fixed.discard(j)
    return fixed


def test_unit_row_criterion_matches_non_negative_bruteforce_on_binary_systems():
    # The RREF unit-row criterion must agree with exhaustive non-negative
    # integer feasibility (the actual domain of freight rates) across many
    # random 0/1 aggregate systems of varying rank.
    rng = random.Random(42)
    for n in range(2, 5):
        for _ in range(60):
            rows, totals = [], []
            secret = [rng.randint(1, 8) for _ in range(n)]
            for _ in range(rng.randint(1, n + 1)):
                row = [rng.randint(0, 1) for _ in range(n)]
                if not any(row):
                    row[rng.randrange(n)] = 1
                rows.append(row)
                totals.append(sum(v * s for v, s in zip(row, secret)))
            engine, _, _ = fixed_boxes(rows, [f'B{i}' for i in range(n)])
            assert engine == brute_force_fixed(rows, totals, n, bound=15)


def _random_system(rng, n):
    rows = rng.randint(1, n + 1)
    coefficients = []
    totals = []
    secret = [rng.randint(1, 9) for _ in range(n)]
    for _ in range(rows):
        row = [rng.randint(0, 1) for _ in range(n)]
        if not any(row):
            row[rng.randrange(n)] = 1
        coefficients.append(row)
        totals.append(sum(v * s for v, s in zip(row, secret)))
    return coefficients, totals, secret


def test_engine_matches_brute_force_on_random_systems():
    rng = random.Random(20300601)
    for n in range(2, 5):
        for _ in range(40):
            coefficients, totals, secret = _random_system(rng, n)
            expected = brute_force_fixed(coefficients, totals, n, bound=10)
            fixed, reduced, dim = fixed_boxes(coefficients, [f'B{i}' for i in range(n)])
            assert fixed == expected, (coefficients, totals, fixed, expected)
            # The real secret must always be a feasible solution.
            for row, total in zip(coefficients, totals):
                assert sum(v * s for v, s in zip(row, secret)) == total


def test_two_group_subtraction_fixes_remaining_box():
    # R1: B1+B2+B3, R2: B1+B2  => B3 is uniquely determined, B1/B2 stay free.
    equations = [[1, 1, 1], [1, 1, 0]]
    fixed, reduced, dim = fixed_boxes(equations, ['B1', 'B2', 'B3'])
    assert fixed == {2}
    assert dim == 1
    basis = nullspace_basis(equations, 3)
    # B1 and B2 move together in the single free direction.
    assert set(basis[1]) == {0, 1}


def test_three_simultaneous_groups_fix_every_box():
    equations = [
        [1, 1, 0],  # B1+B2
        [0, 1, 1],  # B2+B3
        [1, 0, 1],  # B1+B3
    ]
    fixed, _, dim = fixed_boxes(equations, ['B1', 'B2', 'B3'])
    assert fixed == {0, 1, 2}
    assert dim == 0


def test_single_aggregate_does_not_fix_anything():
    fixed, _, dim = fixed_boxes([[1, 1, 1]], ['B1', 'B2', 'B3'])
    assert fixed == set()
    assert dim == 2


def test_rref_uses_exact_rational_arithmetic():
    reduced, pivots = rref([[2, 2, 2], [1, 1, 0]])
    assert all(isinstance(v, Fraction) for row in reduced for v in row)
    # Unit row for the third coordinate still appears despite non-unit pivot.
    unit_rows = [row for row in reduced
                 if len([v for v in row if v != 0]) == 1]
    assert any(row[2] == 1 for row in unit_rows)


def test_empty_system_leaves_all_coordinates_free():
    fixed, _, dim = fixed_boxes([], ['B1', 'B2'])
    assert fixed == set()
    assert dim == 2
    assert nullspace_basis([], 2) == {0: {0: 1}, 1: {1: 1}}
