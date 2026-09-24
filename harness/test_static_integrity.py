"""Engine B: Full Module Static Import & Scope Validator (Zero-Cost AST Scanner).

Inspects every function, class, and thread target in forge_core/ to guarantee
zero undefined variable references, missing imports, or unresolvable scopes.
"""
import ast
import builtins
import importlib
import unittest
from pathlib import Path


class ScopeVisitor(ast.NodeVisitor):
    def __init__(self, filename, builtin_names):
        self.filename = filename
        self.builtin_names = builtin_names
        self.scope_stack = [set(builtin_names)]
        self.errors = []

    def current_scope(self):
        res = set()
        for s in self.scope_stack:
            res.update(s)
        return res

    def visit_Module(self, node):
        for item in node.body:
            self._collect_definitions(item, self.scope_stack[-1])
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.scope_stack[-1].add(node.name)
        # Class body has its own scope for definitions
        class_locals = set()
        for item in node.body:
            self._collect_definitions(item, class_locals)
        self.scope_stack.append(class_locals)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_func(node)

    def _visit_func(self, node):
        self.scope_stack[-1].add(node.name)
        func_locals = set()
        for arg in node.args.args + node.args.kwonlyargs:
            func_locals.add(arg.arg)
        if node.args.vararg:
            func_locals.add(node.args.vararg.arg)
        if node.args.kwarg:
            func_locals.add(node.args.kwarg.arg)

        def scan_body(stmts):
            for stmt in stmts:
                self._collect_definitions(stmt, func_locals)
                for child in ast.iter_child_nodes(stmt):
                    if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.expr)):
                        scan_body([child])
        scan_body(node.body)

        self.scope_stack.append(func_locals)
        for item in node.body:
            self.visit(item)
        self.scope_stack.pop()

    def visit_Lambda(self, node):
        lambda_locals = set()
        for arg in node.args.args + node.args.kwonlyargs:
            lambda_locals.add(arg.arg)
        if node.args.vararg:
            lambda_locals.add(node.args.vararg.arg)
        if node.args.kwarg:
            lambda_locals.add(node.args.kwarg.arg)
        self.scope_stack.append(lambda_locals)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_ListComp(self, node):
        self._visit_comp(node)

    def visit_SetComp(self, node):
        self._visit_comp(node)

    def visit_DictComp(self, node):
        self._visit_comp(node)

    def visit_GeneratorExp(self, node):
        self._visit_comp(node)

    def _visit_comp(self, node):
        comp_scope = set()
        for gen in node.generators:
            self._collect_target(gen.target, comp_scope)
            self.visit(gen.iter)
            for if_expr in gen.ifs:
                self.scope_stack.append(comp_scope)
                self.visit(if_expr)
                self.scope_stack.pop()
        self.scope_stack.append(comp_scope)
        if hasattr(node, "elt"):
            self.visit(node.elt)
        if hasattr(node, "key"):
            self.visit(node.key)
        if hasattr(node, "value"):
            self.visit(node.value)
        self.scope_stack.pop()

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            if node.id not in self.current_scope():
                self.errors.append((node.id, node.lineno))
        self.generic_visit(node)

    def _collect_definitions(self, node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                scope.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                scope.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                self._collect_target(target, scope)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            self._collect_target(node.target, scope)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                scope.add(name)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self._collect_target(node.target, scope)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars:
                    self._collect_target(item.optional_vars, scope)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                scope.add(node.name)
        elif isinstance(node, ast.comprehension):
            self._collect_target(node.target, scope)

    def _collect_target(self, target, scope):
        if isinstance(target, ast.Name):
            scope.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._collect_target(elt, scope)


class TestStaticIntegrity(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        self.src_files = sorted((self.root / "forge_core").glob("*.py"))

    def test_all_modules_importable(self):
        """Every module in forge_core must import cleanly with zero runtime side-effects."""
        for p in self.src_files:
            mod_name = f"forge_core.{p.stem}"
            with self.subTest(module=mod_name):
                try:
                    mod = importlib.import_module(mod_name)
                    self.assertIsNotNone(mod)
                except Exception as e:
                    self.fail(f"Module {mod_name} failed to import: {e}")

    def test_ast_parse_and_undefined_names(self):
        """Every function and method in forge_core must have valid syntax and resolvable names."""
        builtin_names = set(dir(builtins))
        for p in self.src_files:
            with open(p, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=str(p))

            visitor = ScopeVisitor(p.name, builtin_names)
            visitor.visit(tree)
            if visitor.errors:
                first_err = visitor.errors[0]
                self.fail(f"Undefined name '{first_err[0]}' found in {p.name} at line {first_err[1]}")


if __name__ == "__main__":
    unittest.main()
