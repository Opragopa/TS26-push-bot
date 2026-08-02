import unittest
from unittest import mock

import runtime_bootstrap


class RuntimeBootstrapTests(unittest.TestCase):
    def test_dependency_bootstrap_skips_when_modules_exist(self):
        with mock.patch.object(runtime_bootstrap, "missing_runtime_modules", return_value=[]), mock.patch.object(runtime_bootstrap.subprocess, "check_call") as check_call:
            changed = runtime_bootstrap.ensure_runtime_dependencies()
        self.assertFalse(changed)
        check_call.assert_not_called()

    def test_dependency_bootstrap_installs_requirements_when_missing(self):
        with mock.patch.object(runtime_bootstrap, "missing_runtime_modules", side_effect=[["gspread"], []]), mock.patch.object(runtime_bootstrap.subprocess, "check_call") as check_call:
            changed = runtime_bootstrap.ensure_runtime_dependencies()
        self.assertTrue(changed)
        command = check_call.call_args.args[0]
        self.assertIn("-m", command)
        self.assertIn("pip", command)
        self.assertIn("install", command)
        self.assertIn("requirements.txt", command[-1])

    def test_dependency_bootstrap_continues_when_pip_is_missing(self):
        with mock.patch.object(runtime_bootstrap, "missing_runtime_modules", return_value=["gspread"]), mock.patch.object(runtime_bootstrap.importlib.util, "find_spec", return_value=None), mock.patch.object(runtime_bootstrap.subprocess, "check_call") as check_call:
            changed = runtime_bootstrap.ensure_runtime_dependencies()
        self.assertFalse(changed)
        check_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
