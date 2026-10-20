"""
ChronoShrink — Formal Evaluator Contract & Anti-Cheat Regression Suite
Covers invalid subcases (runtime exceptions, NaN/Inf predictions, non-deterministic
state, in-place history window mutation) plus 100% bit-exact lossless controls.
"""

import ast
import math
import unittest
from src.benchmark_engine import BenchmarkEngine


def _audit_code_ast(code_str: str):
    try:
        tree = ast.parse(code_str)
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.While)):
            return False, f"Forbidden AST node: {type(node).__name__}"
    return True, "OK"


class TestEvaluatorContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = BenchmarkEngine(dataset_size=10)

    def _eval_code(self, code: str, name: str):
        ok_ast, msg = _audit_code_ast(code)
        if not ok_ast:
            return {"is_valid": False, "error_msg": msg}
        ns = {"math": math, "abs": abs, "min": min, "max": max, "sum": sum, "len": len, "int": int, "float": float, "round": round}
        exec(code, ns)
        return self.engine.evaluate_candidate(ns["predict_next_value"], name)

    def test_01_runtime_exceptions_and_ast_violations_rejected(self):
        subcases = [
            """
def predict_next_value(history_window, timestamps_delta, channel_correlations):
    if len(history_window) in (5, 3):
        return history_window[-1] + (history_window[-1] - history_window[-2])
    return 1 // 0
""",
            """
def predict_next_value(history_window, timestamps_delta, channel_correlations):
    global _state
    return 0
""",
            """
def predict_next_value(history_window, timestamps_delta, channel_correlations):
    history_window[0] = 999999
    return history_window[-1]
""",
        ]
        for idx, code in enumerate(subcases, 1):
            with self.subTest(subcase=idx):
                res = self._eval_code(code, f"Invalid_{idx}")
                self.assertFalse(res["is_valid"], f"Subcase {idx} must be rejected")

    def test_02_non_finite_and_nondeterministic_rejected(self):
        subcases = [
            """
def predict_next_value(history_window, timestamps_delta, channel_correlations):
    return float('nan')
""",
            """
def predict_next_value(history_window, timestamps_delta, channel_correlations):
    return float('inf')
""",
            """
def predict_next_value(history_window, timestamps_delta, channel_correlations):
    return 42
""",
        ]
        for idx, code in enumerate(subcases, 1):
            with self.subTest(subcase=idx):
                res = self._eval_code(code, f"NonFinite_{idx}")
                self.assertFalse(res["is_valid"], f"Subcase {idx} must be rejected")

    def test_03_valid_lossless_predictor_passes_bit_exact(self):
        valid_code = """
def predict_next_value(history_window, timestamps_delta, channel_correlations):
    if not history_window:
        return 0
    if len(history_window) < 2:
        return history_window[-1]
    return 2 * history_window[-1] - history_window[-2]
"""
        res = self._eval_code(valid_code, "ValidDoubleDelta")
        self.assertTrue(res["is_valid"], f"Valid control failed: {res.get('error_msg')}")
        self.assertGreater(res["compression_ratio_pct"], 50.0)


if __name__ == "__main__":
    unittest.main()
