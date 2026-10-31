"""Offline integration coverage for filesystem skills, memory and call limits."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from agent import main as agent_main
from scripts._verification import ScriptedModel


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "AGENTS.md").write_text("MEMORY_SENTINEL: preserve project rules", encoding="utf-8")
        skill = self.root / ".agents" / "skills" / "example-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: example-review\ndescription: Review local code carefully.\n---\n"
            "SKILL_BODY_SENTINEL: report evidence.\n", encoding="utf-8",
        )
        self.root_patch = patch.object(agent_main, "PROJECT_ROOT", self.root, create=True)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    async def test_memory_skill_reading_and_thread_history(self) -> None:
        model = ScriptedModel(replies=[
            AIMessage(content="", tool_calls=[{
                "name": "read_file", "args": {"file_path": "/.agents/skills/example-review/SKILL.md"},
                "id": "read-skill", "type": "tool_call",
            }]),
            AIMessage(content="done"),
        ])
        with patch.object(agent_main, "build_model", return_value=model):
            agent = agent_main.build_local_agent(user_id="alice", max_model_calls=3)
        config = {"configurable": {"thread_id": "memory-skills-test"}}
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "first-turn"}]}, config)
        self.assertEqual(result["messages"][-1].content, "done")
        first_prompt = str(model.seen_messages[0][0].content)
        self.assertIn("MEMORY_SENTINEL", first_prompt)
        self.assertIn("example-review", first_prompt)
        self.assertTrue(any(
            isinstance(message, ToolMessage) and "SKILL_BODY_SENTINEL" in str(message.content)
            for message in model.seen_messages[1]
        ))
        await agent.ainvoke({"messages": [{"role": "user", "content": "second-turn"}]}, config)
        self.assertTrue(any(message.content == "first-turn" for message in model.seen_messages[-1]))
        other = {"configurable": {"thread_id": "other-thread"}}
        await agent.ainvoke({"messages": [{"role": "user", "content": "separate-turn"}]}, other)
        self.assertFalse(any(message.content == "first-turn" for message in model.seen_messages[-1]))

    async def test_real_project_rules_and_skill_are_discovered(self) -> None:
        root = Path(__file__).resolve().parents[1]
        model = ScriptedModel(replies=[AIMessage(content="done")])
        with patch.object(agent_main, "PROJECT_ROOT", root), patch.object(
            agent_main, "build_model", return_value=model,
        ):
            agent = agent_main.build_local_agent(max_model_calls=1)
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": "hello"}]},
            {"configurable": {"thread_id": "project-rules"}},
        )
        prompt = str(model.seen_messages[0][0].content)
        self.assertIn("通用开发 Agent 工作约定", prompt)
        self.assertIn("my-claw-code-quality", prompt)

    async def test_model_limit_stops_loop_before_next_call(self) -> None:
        model = ScriptedModel(replies=[AIMessage(content="", tool_calls=[{
            "name": "ls", "args": {"path": "/"}, "id": "list-root", "type": "tool_call",
        }])])
        with patch.object(agent_main, "build_model", return_value=model):
            agent = agent_main.build_local_agent(max_model_calls=2)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "keep listing"}]},
            {"configurable": {"thread_id": "limited-run"}},
        )
        self.assertEqual(len(model.seen_messages), 2)
        self.assertIn("limit", str(result["messages"][-1].content).lower())

    async def test_none_means_unlimited_and_invalid_limits_are_rejected(self) -> None:
        model = ScriptedModel(replies=[AIMessage(content="done")])
        with patch.object(agent_main, "build_model", return_value=model):
            agent = agent_main.build_local_agent(max_model_calls=None)
            for invalid in [0, -1, True, 1.5]:
                with self.subTest(limit=invalid), self.assertRaises(ValueError):
                    agent_main.build_local_agent(max_model_calls=invalid)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "hello"}]},
            {"configurable": {"thread_id": "unlimited-run"}},
        )
        self.assertEqual(result["messages"][-1].content, "done")


if __name__ == "__main__":
    unittest.main()
