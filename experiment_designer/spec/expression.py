"""
Safe Expression Evaluator - Evaluate scoring expressions without using eval().

Supports:
- Basic arithmetic operators: +, -, *, /, ** (power)
- Safe math functions: abs, pow, sqrt, min, max, log, exp
- Variable substitution: {crawler.pi}, {item.pi}, {alpha}
- Nested attribute access: {crawler.configuration.params.pi}

Security:
- Uses AST parsing instead of eval()
- Whitelist of allowed operations
- No arbitrary code execution
"""

import ast
import math
import operator
import re
from typing import Any, Callable, Dict, List, Optional, Union


# Allowed binary operators
BINARY_OPS: Dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# Allowed unary operators
UNARY_OPS: Dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Allowed comparison operators
COMPARE_OPS: Dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

# Safe math functions
SAFE_FUNCTIONS: Dict[str, Callable] = {
    'abs': abs,
    'pow': pow,
    'sqrt': math.sqrt,
    'min': min,
    'max': max,
    'log': math.log,
    'log10': math.log10,
    'exp': math.exp,
    'ceil': math.ceil,
    'floor': math.floor,
    'round': round,
    'int': int,
    'float': float,
}


class ExpressionError(Exception):
    """Raised when expression evaluation fails."""
    pass


class SafeExpressionEvaluator:
    """
    Safely evaluate mathematical expressions.

    Example usage:
        evaluator = SafeExpressionEvaluator()

        # Simple expression
        result = evaluator.evaluate("2 + 3 * 4")  # 14

        # With variables
        result = evaluator.evaluate(
            "{alpha} - {tau} * pow(abs({crawler.pi} - {item.pi}), 2)",
            {"alpha": 0.35, "tau": 1, "crawler": {"pi": -0.5}, "item": {"pi": 0.2}}
        )
    """

    # Pattern to match variable references like {var} or {obj.attr}
    VAR_PATTERN = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\}')

    def __init__(self, functions: Optional[Dict[str, Callable]] = None):
        """
        Initialize evaluator.

        Args:
            functions: Additional safe functions to allow
        """
        self.functions = {**SAFE_FUNCTIONS}
        if functions:
            self.functions.update(functions)

    def evaluate(
        self,
        expression: str,
        context: Optional[Dict[str, Any]] = None
    ) -> Union[float, int, bool]:
        """
        Evaluate a mathematical expression.

        Args:
            expression: Expression string (e.g., "2 + 3 * {x}")
            context: Dict of variable values

        Returns:
            Evaluation result

        Raises:
            ExpressionError: If expression is invalid or evaluation fails
        """
        context = context or {}

        # Substitute variables
        expr_with_values = self._substitute_variables(expression, context)

        try:
            # Parse expression as AST
            tree = ast.parse(expr_with_values, mode='eval')

            # Evaluate AST
            return self._eval_node(tree.body)

        except SyntaxError as e:
            raise ExpressionError(f"Invalid expression syntax: {expression} -> {expr_with_values}") from e
        except Exception as e:
            raise ExpressionError(f"Evaluation error: {e}") from e

    def _substitute_variables(self, expression: str, context: Dict[str, Any]) -> str:
        """
        Substitute variable references with their values.

        Handles:
        - Simple variables: {alpha} -> 0.35
        - Nested attributes: {crawler.pi} -> -0.5
        - Nested dicts: {crawler.configuration.params.pi}
        """
        def replace_var(match: re.Match) -> str:
            var_path = match.group(1)
            value = self._get_nested_value(context, var_path)

            if value is None:
                raise ExpressionError(f"Variable not found: {var_path}")

            # Format number to avoid scientific notation issues
            if isinstance(value, float):
                return repr(value)
            return str(value)

        return self.VAR_PATTERN.sub(replace_var, expression)

    def _get_nested_value(self, obj: Any, path: str) -> Any:
        """
        Get a value from a nested dict using dot notation.

        Args:
            obj: Root object (dict or object with attributes)
            path: Dot-separated path (e.g., "crawler.configuration.params.pi")

        Returns:
            Value at path or None if not found
        """
        parts = path.split('.')
        current = obj

        for part in parts:
            if current is None:
                return None

            if isinstance(current, dict):
                current = current.get(part)
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return None

        return current

    def _eval_node(self, node: ast.AST) -> Any:
        """
        Recursively evaluate an AST node.

        Args:
            node: AST node to evaluate

        Returns:
            Evaluation result

        Raises:
            ExpressionError: If node type is not allowed
        """
        if isinstance(node, ast.Constant):
            # Constant covers numbers, strings, booleans and None. The old
            # ast.Num/ast.Str aliases were removed in Python 3.12.
            return node.value

        elif isinstance(node, ast.BinOp):
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            op_type = type(node.op)

            if op_type not in BINARY_OPS:
                raise ExpressionError(f"Unsupported operator: {op_type.__name__}")

            return BINARY_OPS[op_type](left, right)

        elif isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand)
            op_type = type(node.op)

            if op_type not in UNARY_OPS:
                raise ExpressionError(f"Unsupported unary operator: {op_type.__name__}")

            return UNARY_OPS[op_type](operand)

        elif isinstance(node, ast.Compare):
            # Handle comparisons like a < b < c
            left = self._eval_node(node.left)
            result = True

            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval_node(comparator)
                op_type = type(op)

                if op_type not in COMPARE_OPS:
                    raise ExpressionError(f"Unsupported comparison: {op_type.__name__}")

                if not COMPARE_OPS[op_type](left, right):
                    result = False
                    break

                left = right

            return result

        elif isinstance(node, ast.Call):
            # Function call
            if not isinstance(node.func, ast.Name):
                raise ExpressionError("Only simple function calls are allowed")

            func_name = node.func.id

            if func_name not in self.functions:
                raise ExpressionError(f"Function not allowed: {func_name}")

            args = [self._eval_node(arg) for arg in node.args]
            return self.functions[func_name](*args)

        elif isinstance(node, ast.Name):
            # Variable reference (shouldn't happen after substitution)
            raise ExpressionError(f"Unknown variable: {node.id}")

        elif isinstance(node, ast.IfExp):
            # Ternary: a if condition else b
            condition = self._eval_node(node.test)
            if condition:
                return self._eval_node(node.body)
            else:
                return self._eval_node(node.orelse)

        elif isinstance(node, ast.BoolOp):
            # and/or
            if isinstance(node.op, ast.And):
                result = True
                for value in node.values:
                    result = result and self._eval_node(value)
                    if not result:
                        break
                return result
            elif isinstance(node.op, ast.Or):
                result = False
                for value in node.values:
                    result = result or self._eval_node(value)
                    if result:
                        break
                return result

        raise ExpressionError(f"Unsupported expression type: {type(node).__name__}")

    def validate(self, expression: str) -> List[str]:
        """
        Validate an expression without evaluating it.

        Args:
            expression: Expression string

        Returns:
            List of error messages (empty if valid)
        """
        errors = []

        # Extract variable names
        var_matches = self.VAR_PATTERN.findall(expression)

        # Replace variables with dummy values for syntax checking
        test_expr = self.VAR_PATTERN.sub('1.0', expression)

        try:
            tree = ast.parse(test_expr, mode='eval')
            self._validate_node(tree.body, errors)
        except SyntaxError as e:
            errors.append(f"Syntax error: {e}")

        return errors

    def _validate_node(self, node: ast.AST, errors: List[str]) -> None:
        """Recursively validate an AST node."""
        if isinstance(node, ast.Constant):
            pass
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in BINARY_OPS:
                errors.append(f"Unsupported operator: {type(node.op).__name__}")
            self._validate_node(node.left, errors)
            self._validate_node(node.right, errors)
        elif isinstance(node, ast.UnaryOp):
            if type(node.op) not in UNARY_OPS:
                errors.append(f"Unsupported unary operator: {type(node.op).__name__}")
            self._validate_node(node.operand, errors)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in self.functions:
                    errors.append(f"Unknown function: {node.func.id}")
            else:
                errors.append("Only simple function calls are allowed")
            for arg in node.args:
                self._validate_node(arg, errors)
        elif isinstance(node, ast.Compare):
            self._validate_node(node.left, errors)
            for op, comp in zip(node.ops, node.comparators):
                if type(op) not in COMPARE_OPS:
                    errors.append(f"Unsupported comparison: {type(op).__name__}")
                self._validate_node(comp, errors)
        elif isinstance(node, ast.IfExp):
            self._validate_node(node.test, errors)
            self._validate_node(node.body, errors)
            self._validate_node(node.orelse, errors)
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                self._validate_node(value, errors)
        elif isinstance(node, ast.Name):
            # After substitution, no variables should remain
            pass
        else:
            errors.append(f"Unsupported expression type: {type(node).__name__}")

    def extract_variables(self, expression: str) -> List[str]:
        """
        Extract variable names from an expression.

        Args:
            expression: Expression string

        Returns:
            List of variable paths (e.g., ["alpha", "crawler.pi", "item.pi"])
        """
        return self.VAR_PATTERN.findall(expression)


# Convenience function for simple evaluation
def evaluate_expression(
    expression: str,
    context: Optional[Dict[str, Any]] = None
) -> Union[float, int, bool]:
    """
    Evaluate a mathematical expression.

    This is a convenience function that creates a SafeExpressionEvaluator
    and evaluates the expression.

    Args:
        expression: Expression string
        context: Dict of variable values

    Returns:
        Evaluation result
    """
    evaluator = SafeExpressionEvaluator()
    return evaluator.evaluate(expression, context)
