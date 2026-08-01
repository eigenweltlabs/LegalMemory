"""Agentic mode over the vendored sandbox + skills, with our retrieval tools bridged in.

Reuses the vendored ``ToolExecutor`` (bash/read/write/edit/glob/grep over the Podman
sandbox) and ``skills/`` (real .docx/.xlsx production) verbatim; the only new code is
the retrieval bridge — our ``search_*/get_document/traverse`` registered *inside* their
executor so the agent finds its own documents from the firm index instead of being
handed them. The deliverable files the agent writes to ``/workspace/output`` are read
back and judged.

Import is deferred everywhere heavy (Sandbox, retrieval) so the module stays light; the
sandbox itself only spins up when ``run_agentic_sandbox`` is called.
"""

from __future__ import annotations

from pathlib import Path

from knowledge_index.benchmark.agent import ProducedWork, Task, ToolSuite, _output_spec
from knowledge_index.config import AppConfig

_RETRIEVAL_NAMES = {"search_filter", "search_semantic", "list_matters", "get_document", "traverse"}

# the harness's canonical (flat) tool-def format, so loop/adapter handle them uniformly.
RETRIEVAL_TOOL_DEFINITIONS = [
    {
        "name": "search_filter",
        "description": "List firm documents by exact metadata (matter_id, doc_type, "
        "version_status). Use first when the matter or type is known.",
        "parameters": {
            "type": "object",
            "properties": {
                "matter_id": {"type": "string"},
                "doc_type": {"type": "string"},
                "version_status": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "search_semantic",
        "description": "Hybrid semantic + lexical search over the firm's document chunks; "
        "accepts the same metadata pre-filters.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "matter_id": {"type": "string"},
                "doc_type": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_matters",
        "description": "List matters visible to you with reference numbers and practice area.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_document",
        "description": "Fetch one firm document's full structured content by document_id.",
        "parameters": {
            "type": "object",
            "properties": {"document_id": {"type": "string"}},
            "required": ["document_id"],
        },
    },
    {
        "name": "traverse",
        "description": "Walk typed relations (version chain, annexes, thread) from a document_id.",
        "parameters": {
            "type": "object",
            "properties": {"document_id": {"type": "string"}},
            "required": ["document_id"],
        },
    },
]

_HARNESS = Path(__file__).parent / "agent_harness"
_SKILLS_DIR = _HARNESS / "skills"

# Retrieval-first preamble: the upstream harness assumes documents are pre-staged on disk;
# ours are retrieval-only, so we replace that framing (the one thing that must change)
# and keep their skill manuals verbatim (so the agent produces real binary deliverables).
_WORKSPACE_PREAMBLE = (
    "You are a legal associate completing an assignment in a sandbox workspace. `bash` "
    "starts in your workspace; deliverables go in the `output/` directory.\n\n"
    "## Finding documents (IMPORTANT)\n"
    "The firm's documents are NOT on disk — `documents/` is EMPTY. Find them with these "
    "tools:\n"
    "- `search_semantic(query=...)` — hybrid natural-language search. THIS IS YOUR "
    "PRIMARY TOOL; use plain-language queries (e.g. 'ISDA schedule credit support annex "
    "draft'). Start here and search broadly.\n"
    "- `list_matters` — your matters and their ids.\n"
    "- `get_document(document_id=...)` — a document's full content by id (ids come from "
    "search results).\n"
    "- `traverse(document_id=...)` — version chains, annexes, threads.\n"
    "- `search_filter` — ONLY if you know exact taxonomy ids; do NOT guess `doc_type` or "
    "other values (guesses return nothing). Prefer `search_semantic`.\n"
    "Retrieve everything you need FIRST; never assume a file is on disk and never invent "
    "parties, numbers, or terms.\n\n"
    "## Producing deliverables (you CAN do this — never refuse)\n"
    "You have a `bash` shell and the docx/xlsx skills ARE installed at "
    "`skills/<name>/scripts/`. To produce a real .docx: (1) draft the content as markdown "
    "with the `write` tool (e.g. `write memo.md`); (2) convert it via bash, e.g. "
    "`bash python skills/docx/scripts/generate_from_md.py memo.md output/memo.docx`; "
    "(3) do the same for each requested deliverable, then validate with "
    "`skills/docx/scripts/validate.py`. Read the skill manuals below for redlines/"
    "templates. NEVER claim you cannot generate binary files — you can, via these skills. "
    "Every requested output file MUST end up in `output/` as a real .docx/.xlsx.\n"
)


def _skill_names() -> list[str]:
    return sorted(path.parent.name for path in _SKILLS_DIR.glob("*/SKILL.md"))


def _build_system_prompt() -> str:
    """Retrieval-first preamble + every vendored SKILL.md manual (verbatim)."""
    manuals = "\n".join(
        f"\n\n## Skill: {name}\n\n{(_SKILLS_DIR / name / 'SKILL.md').read_text(encoding='utf-8')}"
        for name in _skill_names()
    )
    return _WORKSPACE_PREAMBLE + manuals


def _stage_skills(workspace_dir: Path) -> None:
    """Copy skill scripts into the workspace so the agent can invoke them via bash."""
    import shutil

    for name in _skill_names():
        scripts = _SKILLS_DIR / name / "scripts"
        if scripts.exists():
            dest = workspace_dir / "skills" / name / "scripts"
            shutil.copytree(scripts, dest, dirs_exist_ok=True)


class RetrievalToolExecutor:
    """The vendored ToolExecutor with our retrieval tools bridged in.

    Delegates the file/bash/skill tools to the real vendored ``ToolExecutor`` and
    the retrieval tools to a ``ToolSuite`` (our RetrievalService bridge). Exposes the
    ``.execute`` / ``.get_metrics`` duck-type the agent loop expects.
    """

    def __init__(self, service, principal: str, *, documents_dir, output_dir, workspace_dir):
        from knowledge_index.benchmark.agent_harness.tools import ToolExecutor

        self.suite = ToolSuite(service, principal)
        self.files = ToolExecutor(
            documents_dir=str(documents_dir),
            output_dir=str(output_dir),
            workspace_dir=str(workspace_dir),
        )

    @property
    def retrieved_paths(self) -> set[str]:
        return self.suite.retrieved_paths

    def execute(self, tool_name: str, arguments) -> str:
        if tool_name in _RETRIEVAL_NAMES:
            return self.suite.execute(tool_name, arguments)
        return self.files.execute(tool_name, arguments)

    def get_metrics(self) -> dict:
        return {**self.files.get_metrics(), **self.suite.get_metrics()}

    def close(self) -> None:
        self.files.close()


def run_agentic_sandbox(
    task: Task,
    service,
    config: AppConfig,
    model: str,
    *,
    work_root: str,
    max_steps: int = 25,
) -> ProducedWork:
    """Run the agent loop over the real sandbox; the deliverable files are the output."""
    from knowledge_index.benchmark.gateway_adapter import GatewayAdapter
    from knowledge_index.benchmark.agent_harness.agent_loop import run_agent
    from knowledge_index.benchmark.agent_harness.tools import get_all_tool_definitions

    base = Path(work_root) / task.scenario_id.replace("/", "_")
    documents = base / "documents"  # left EMPTY: agentic mode must retrieve, not be handed docs
    output = base / "output"
    workspace = base / "work"
    for directory in (documents, output, workspace):
        directory.mkdir(parents=True, exist_ok=True)
    _stage_skills(workspace)  # copy skill scripts into the sandbox workspace

    executor = RetrievalToolExecutor(
        service, task.principal, documents_dir=documents, output_dir=output, workspace_dir=workspace
    )
    try:
        tools = get_all_tool_definitions() + RETRIEVAL_TOOL_DEFINITIONS
        result = run_agent(
            GatewayAdapter(config, model),
            _build_system_prompt(),
            _output_spec(task),
            executor,
            tools=tools,
            max_turns=max_steps,
            transcript_path=str(base / "transcript.jsonl"),
        )
        work_product = _read_output_files(output)
        if not work_product.strip():
            # agent answered in chat instead of writing files — judge its final text
            for message in reversed(result["messages"]):
                if message.get("role") == "assistant" and message.get("content"):
                    work_product = message["content"]
                    break
        retrieved = set(executor.retrieved_paths)
        tool_calls = executor.suite.calls
    finally:
        executor.close()
    return ProducedWork("agentic", work_product, retrieved, tool_calls, result["turn_count"])


def _read_output_files(output_dir: Path) -> str:
    """Concatenate the agent's deliverables as text, using the vendored file readers."""
    from knowledge_index.benchmark.agent_harness.scoring import _read_file_as_text

    parts: list[str] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = _read_file_as_text(path)
        except Exception:
            text = ""
        if not text.strip():  # not a valid binary doc — fall back to raw text
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
        parts.append(f"=== {path.name} ===\n{text}")
    return "\n\n".join(parts)
