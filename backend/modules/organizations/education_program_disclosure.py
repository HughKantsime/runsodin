"""Exact disclosure checks for the report's summary/program/detail equations.

Substituting program = classes + Unassigned and summary = programs expresses
all released cells as vectors over detail cells. RREF gives a complete basis of
what those releases determine, including mixed-level subtractions. The scope
sets form a laminar family (nested or disjoint), so its reduced basis consists
of disjoint 0/1 atoms. Every determined cohort is a union of these atoms: testing
the atoms is sufficient, without enumerating exponentially many subsets.

The virtual non-concentrator metric is eliminated from a JOINT P/C system.
This also catches differences whose participants and concentrators are inferred
from different released rows. Exact Fractions avoid floating-point rank tests.
"""
from fractions import Fraction

PEOPLE = ('participants', 'concentrators')
KNOWN_ZERO = (*PEOPLE, 'instructor_entered_teaching_hours')


def _subtract(target, source, factor):
    for column, value in source.items():
        remaining = target.get(column, 0) - factor * value
        if remaining:
            target[column] = remaining
        else:
            target.pop(column, None)


def _reduce(equations, order):
    """Sparse exact RREF, carrying the released-cell witness for each row."""
    basis = {}
    # Eliminate the smallest scopes first. Starting with a school-wide total
    # creates dense intermediate witnesses when hundreds of classes are public.
    for vector, witness in sorted(equations, key=lambda item: len(item[0])):
        vector, witness = dict(vector), dict(witness)
        for pivot in sorted(basis, key=order):
            previous, evidence = basis[pivot]
            factor = vector.get(pivot, 0)
            if factor:
                _subtract(vector, previous, factor)
                _subtract(witness, evidence, factor)
        if not vector:
            continue
        pivot = min(vector, key=order)
        scale = vector[pivot]
        vector = {i: Fraction(v) / scale for i, v in vector.items()}
        witness = {i: Fraction(v) / scale for i, v in witness.items()}
        for previous, evidence in basis.values():
            factor = previous.get(pivot, 0)
            if factor:
                _subtract(previous, vector, factor)
                _subtract(evidence, witness, factor)
        basis[pivot] = (vector, witness)
    return [basis[p] for p in sorted(basis, key=order)]


class DisclosureSystem:
    def __init__(self, report, rows):
        self.rows = rows
        self.leaves = report['classes']
        self.values = [dict(row) for row in rows]
        leaf_ids = {id(row): i for i, row in enumerate(self.leaves)}
        self.scopes = []
        for row in rows:
            if row is report['summary']:
                scope = range(len(self.leaves))
            elif id(row) in leaf_ids:
                scope = [leaf_ids[id(row)]]
            else:
                scope = [i for i, leaf in enumerate(self.leaves) if leaf['cost_center_id'] == row['id']]
            self.scopes.append(dict.fromkeys(scope, 1))

    def _equations(self, key):
        equations = []
        for index, (row, scope) in enumerate(zip(self.rows, self.scopes)):
            # Unassigned cannot have class marks or teaching-ledger entries.
            # Hiding the printed zero does not make that value unknown.
            zero = key in KNOWN_ZERO and 'cost_center_id' in row and row.get('id') is None
            if zero:
                equations.append((scope, {}))
            elif isinstance(row[key], (int, float)):
                equations.append((scope, {(index, key): 1}))
        return equations

    def _atoms(self, key):
        if key != 'non_concentrators':
            return _reduce(self._equations(key), lambda i: i)
        n = len(self.leaves)
        equations = []
        # Variables are N and C, with P = N + C. Eliminate C first;
        # rows left purely in N span every determined non-concentrator cohort.
        for vector, evidence in self._equations('participants'):
            equations.append(({**vector, **{i + n: v for i, v in vector.items()}}, evidence))
        for vector, evidence in self._equations('concentrators'):
            equations.append(({i + n: v for i, v in vector.items()}, evidence))
        return [(v, e) for v, e in _reduce(equations, lambda i: (i < n, i)) if all(i < n for i in v)]

    def protect(self, metrics, hidden, hide, coverage):
        """Remove the smallest involved release, then rebuild to a fixed point.

        A witness names precisely the visible cells used by an unsafe equation;
        structural zeros are never candidates. Ties use stable report order.
        Existing local/status checks run in the caller's same monotone loop.
        """
        for key in (*metrics, 'non_concentrators'):
            columns = PEOPLE if key == 'non_concentrators' else (key,)
            if not any(hidden(row[column]) for row in self.rows for column in columns):
                continue  # no concealed cell can be reconstructed for this metric
            for atom, witness in self._atoms(key):
                if not witness:
                    continue
                # Laminar scopes guarantee disjoint unit atoms. Fail closed if
                # a future report shape violates that premise.
                unsafe = any(coefficient != 1 for coefficient in atom.values())
                if key in (*PEOPLE, 'non_concentrators'):
                    value = sum(coefficient * Fraction(str(self.values[i][metric]))
                                for (i, metric), coefficient in witness.items())
                    unsafe |= 0 < value < 5
                else:
                    concealed = [self.leaves[i] for i in atom if hidden(self.leaves[i][key])
                                 and not (key in KNOWN_ZERO and self.leaves[i].get('id') is None)]
                    unsafe |= bool(concealed) and (len(concealed) == 1 or coverage(concealed) < 5)
                if unsafe:
                    candidates = [(self.rows[i][metric], i, metric) for i, metric in witness
                                  if isinstance(self.rows[i][metric], (int, float))]
                    if candidates:
                        _, index, metric = min(candidates)
                        return hide(self.rows[index], metric)
        return False
