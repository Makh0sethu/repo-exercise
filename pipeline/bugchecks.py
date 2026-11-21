"""Built-in bug checks: static, offline, no dependencies. They parse Python source and never run it.

Each check has a stable code so a finding can be searched for, suppressed in review, and explained.
Add a check by writing a function that yields (line, code, message) from an ast tree and listing it in CHECKS.
"""
import ast

# code -> (severity, short name). Severity reflects "likely a real bug", not "exploitable".
RULES = {
    "BC001": ("moderate", "Bare except hides every error, including Ctrl+C"),
    "BC002": ("moderate", "Mutable default argument is shared between calls"),
    "BC003": ("low", "Compare to None with 'is', not == or !="),
    "BC004": ("moderate", "assert on a tuple is always true"),
    "BC005": ("moderate", "'is' compared to a literal; use =="),
    "BC006": ("low", "Exception swallowed with pass"),
    "BC007": ("high", "eval/exec runs arbitrary code"),
    "BC008": ("moderate", "DEBUG is switched on in source"),
    "BC009": ("moderate", "Hard-coded SECRET_KEY / password-like constant"),
    "BC010": ("low", "Unreachable code after return/raise/break/continue"),
    "BC011": ("moderate", "Loop variable captured by a lambda or closure"),
}

_MUTABLE = (ast.List, ast.Dict, ast.Set)
_MUTABLE_CALLS = {"list", "dict", "set", "defaultdict", "OrderedDict", "deque"}
_SECRETISH = ("secret_key", "password", "passwd", "api_key", "token")


def _is_literal(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes, int, float)) and not isinstance(node.value, bool)


def _name_of(node):
    return node.id if isinstance(node, ast.Name) else (node.attr if isinstance(node, ast.Attribute) else "")


def check_tree(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                yield node.lineno, "BC001"
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                yield node.lineno, "BC006"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for d in [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]:
                if isinstance(d, _MUTABLE) or (isinstance(d, ast.Call) and _name_of(d.func) in _MUTABLE_CALLS):
                    yield d.lineno, "BC002"
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(right, ast.Constant) and right.value is None:
                    yield node.lineno, "BC003"
                if isinstance(op, (ast.Is, ast.IsNot)) and (_is_literal(right) or _is_literal(node.left)):
                    yield node.lineno, "BC005"
        elif isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Tuple) and node.test.elts:
                yield node.lineno, "BC004"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
                yield node.lineno, "BC007"
        elif isinstance(node, ast.Assign):
            targets = [_name_of(t) for t in node.targets]
            if "DEBUG" in targets and isinstance(node.value, ast.Constant) and node.value.value is True:
                yield node.lineno, "BC008"
            for t in targets:
                if any(k in t.lower() for k in _SECRETISH) and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, str) and len(node.value.value) >= 8 \
                        and not node.value.value.startswith(("<", "$", "{")):
                    yield node.lineno, "BC009"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.For, ast.While, ast.If, ast.With, ast.Try)):
            for body in (getattr(node, "body", []), getattr(node, "orelse", []), getattr(node, "finalbody", [])):
                for i, stmt in enumerate(body[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                        yield body[i + 1].lineno, "BC010"
                        break
        if isinstance(node, (ast.For, ast.AsyncFor)):
            names = {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
            for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(inner, ast.Lambda):
                    used = {n.id for n in ast.walk(inner.body) if isinstance(n, ast.Name)}
                    bound = {a.arg for a in inner.args.args}
                    if (used & names) - bound:
                        yield inner.lineno, "BC011"


def check_source(text, filename="<string>"):
    """Return [(line, code)] for one file; a file that doesn't parse is itself reported as BC000."""
    try:
        tree = ast.parse(text, filename=filename)
    except (SyntaxError, ValueError) as exc:
        return [(getattr(exc, "lineno", 1) or 1, "BC000")]
    return sorted(set(check_tree(tree)))


RULES["BC000"] = ("low", "File does not parse as Python 3")
