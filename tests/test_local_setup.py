import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from costflow.config import Settings, read_dotenv
from costflow.errors import AppError


def load_tool(name):
    path = Path(__file__).resolve().parents[1] / "tools" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load_tool("setup_local")
check = load_tool("check_connection")


class LocalSetupTests(unittest.TestCase):
    def test_browser_and_malformed_keys_rejected(self):
        for key in ("", "sb_publishable_example", "a.W10.c", "a.@@.c", "a.e30.c"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                setup.validate_key(key)
        payload = base64.urlsafe_b64encode(json.dumps({"role": "anon"}).encode()).decode()
        with self.assertRaises(ValueError):
            setup.validate_key("header." + payload + ".signature")

    def test_service_role_type_supported(self):
        payload = base64.urlsafe_b64encode(json.dumps({"role": "service_role"}).encode()).decode()
        value = "header." + payload + ".signature"
        self.assertEqual(setup.validate_key(value), value)

    def test_env_preserves_settings_and_rejects_injection(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            path = Path(folder) / ".env"
            path.write_text("EXISTING=value\n", encoding="utf-8")
            setup.save_env(path, {"SUPABASE_SERVICE_KEY": "local-test-placeholder"})
            self.assertEqual(read_dotenv(path)["EXISTING"], "value")
            previous = path.read_bytes()
            with self.assertRaises(ValueError):
                setup.save_env(path, {"SUPABASE_SERVICE_KEY": "value\nINJECTED=1"})
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual([p.name for p in Path(folder).iterdir()], [".env"])

    def test_readonly_preflight_checks_all_tables_and_private_storage(self):
        class Gateway:
            def __init__(self, public):
                self.public, self.tables, self.paths = public, [], []

            def rows(self, table, limit):
                self.tables.append(table)
                return []

            def request(self, method, path):
                self.assert_readonly = method == "GET"
                self.paths.append(path)
                return {"public": self.public} if "/bucket/" in path else {"id": "test-id"}

        gateway = Gateway(False)
        settings = Settings(users=frozenset({"test-id"}))
        check.verify(gateway, settings)
        self.assertEqual(tuple(gateway.tables), check.TABLES)
        self.assertTrue(gateway.assert_readonly)
        self.assertIn("/auth/v1/admin/users/test-id", gateway.paths)
        with self.assertRaises(AppError):
            check.verify(Gateway(True), settings)


if __name__ == "__main__":
    unittest.main()
