"""Day-one smoke test: every Lambda handler must at least byte-compile under 3.12."""

import pathlib
import py_compile
import unittest

LAMBDAS = pathlib.Path(__file__).resolve().parent.parent


class HandlersCompile(unittest.TestCase):
    def test_every_handler_compiles(self):
        handlers = sorted(LAMBDAS.glob("*/handler.py"))
        self.assertTrue(handlers, "no lambdas/*/handler.py found")
        for path in handlers:
            with self.subTest(handler=path.relative_to(LAMBDAS)):
                py_compile.compile(str(path), doraise=True)


if __name__ == "__main__":
    unittest.main()
