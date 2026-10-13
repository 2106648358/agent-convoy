from setuptools import setup


setup(
    name="agent-convoy",
    version="0.2.0",
    description="Local-first conversation migration between Codex CLI, Claude Code, and WorkBuddy",
    py_modules=["agent_convoy", "agent_convoy_web"],
    python_requires=">=3.9",
    entry_points={"console_scripts": ["agent-convoy=agent_convoy:main"]},
)
