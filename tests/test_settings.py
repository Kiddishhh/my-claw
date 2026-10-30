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


def test_langsmith_environment_respects_overrides(tmp_path, monkeypatch) -> None:
    """追踪配置补入环境并刷新缓存；保留项目名覆盖，不导出模型密钥。"""
    import os
    from infra.settings import load_langsmith_environment
    from langsmith import utils
    path = tmp_path / ".env"
    path.write_text("LANGSMITH_TRACING=true\nLANGSMITH_API_KEY=test-secret\nLANGSMITH_PROJECT=demo\nSYC_API_KEY=model-secret\n", encoding="utf-8")
    with monkeypatch.context() as env:
        for key in ("LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "SYC_API_KEY"):
            env.delenv(key, raising=False)
        env.delenv("LANGSMITH_TRACING", raising=False)
        env.setenv("LANGSMITH_PROJECT", "override-project")
        utils.get_env_var("API_KEY")
        try:
            load_langsmith_environment(path)
            assert os.environ["LANGSMITH_TRACING"] == "true"
            assert utils.get_env_var("API_KEY") == "test-secret"
            assert os.environ["LANGSMITH_PROJECT"] == "override-project"
            assert "SYC_API_KEY" not in os.environ
        finally:
            os.environ.pop("LANGSMITH_API_KEY", None)
            os.environ.pop("LANGSMITH_PROJECT", None)
    utils.get_env_var.cache_clear()
