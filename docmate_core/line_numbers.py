"""Preserve document line numbers and fill gaps with unique row identifiers."""
from decimal import Decimal, InvalidOperation


def ensure_line_numbers(items):
    rows = [row for row in items if isinstance(row, dict)]

    def number(row):
        for key in ('lineNo', 'lineNumber', 'line_no', 'line_number'):
            try:
                value = Decimal(str(row.get(key)))
                if value.is_finite() and value > 0 and value == value.to_integral_value():
                    return int(value)
            except (InvalidOperation, ValueError, TypeError):
                pass
        return None

    numbers = [number(row) for row in rows]
    used = {value for value in numbers if value is not None}
    for index, (row, value) in enumerate(zip(rows, numbers), start=1):
        if value is None:
            value = index
            while value in used:
                value += 1
            used.add(value)
        row['lineNo'] = value
    return rows
