class CalcError(Exception):
    """Raised for malformed expressions and math errors."""


def evaluate(expression: str, variables: dict[str, float] | None = None) -> float:
    """Evaluate an arithmetic expression.

    Supports numbers (including decimals and exponents such as 1.5e3), variables,
    + - * / % and ** (right-associative, binds tighter than unary minus),
    unary + and -, parentheses, and the functions min(a, b, ...), max(a, b, ...)
    and abs(x).
    """
    raise NotImplementedError
