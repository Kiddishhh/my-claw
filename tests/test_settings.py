"""Configuration contract tests for project-level defaults."""

import unittest

from infra.settings import Settings


class SettingsDefaultsTests(unittest.TestCase):
    def test_default_postgres_url_uses_my_claw_identifier(self) -> None:
        settings = Settings(api_key="test-key", _env_file=None)

        self.assertEqual(
            settings.postgres_url,
            "postgresql://my_claw:my_claw@localhost:5432/my_claw",
        )


if __name__ == "__main__":
    unittest.main()
