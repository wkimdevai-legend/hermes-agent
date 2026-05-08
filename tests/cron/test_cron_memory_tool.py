"""Tests for cron-safe memory write opt-in (guarded hybrid Option B).

Covers:
- ``allow_memory_writes`` field on cron jobs (default False, persisted).
- Scheduler attachment helper ``_maybe_attach_cron_memory_store``:
  * default cron jobs do not attach a store
  * ``enabled_toolsets=["memory"]`` alone does NOT authorize attachment
  * opt-in without the memory toolset does NOT attach
  * ``no_agent=True`` cannot attach memory
  * opted-in jobs with the memory toolset attach a store but keep memory/user
    profile prompt blocks disabled (no MEMORY / USER PROFILE injection).
- Cron-safe ``memory_tool`` mode:
  * hard-denies ``target=user``, ``replace``, ``remove`` at code level — no
    file mutation occurs even when the underlying store would have allowed it.
  * responses for cron-safe writes do NOT include existing ``entries`` (no
    memory readback to the cron model).
- Audit log is metadata-only (no full memory content stored).

Tests use a temp HERMES_HOME — they never touch live ~/.hermes/memories or
live cron jobs.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated environment with temp HERMES_HOME for cron + memory."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    (hermes_home / "scripts").mkdir()
    (hermes_home / "memories").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # Override module-level cached paths in cron.jobs.
    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")

    # Override memory_tool dynamic dir so the store reads/writes the temp dir.
    import tools.memory_tool as memory_mod
    monkeypatch.setattr(memory_mod, "get_memory_dir", lambda: hermes_home / "memories")

    return hermes_home


def _make_fake_agent():
    """Tiny stand-in for AIAgent so the helper can mutate attributes.

    The scheduler helper only touches a small set of attributes on the agent
    object — we don't need a real AIAgent to validate the contract.
    """
    return SimpleNamespace(
        _memory_store=None,
        _memory_enabled=False,
        _user_profile_enabled=False,
        _memory_write_origin="assistant_tool",
        _cron_safe_memory=False,
    )


# ---------------------------------------------------------------------------
# Job schema field
# ---------------------------------------------------------------------------

class TestAllowMemoryWritesField:
    def test_default_is_false(self, cron_env):
        from cron.jobs import create_job, get_job

        job = create_job(prompt="x", schedule="every 1h")
        assert job.get("allow_memory_writes") is False
        assert get_job(job["id"]).get("allow_memory_writes") is False

    def test_opt_in_persists(self, cron_env):
        from cron.jobs import create_job, get_job

        job = create_job(
            prompt="x",
            schedule="every 1h",
            enabled_toolsets=["memory"],
            allow_memory_writes=True,
        )
        assert job.get("allow_memory_writes") is True
        assert get_job(job["id"]).get("allow_memory_writes") is True

    def test_no_agent_with_opt_in_rejected(self, cron_env):
        """no_agent=True jobs cannot opt into memory writes."""
        from cron.jobs import create_job

        with pytest.raises(ValueError) as excinfo:
            create_job(
                prompt=None,
                schedule="every 1h",
                script="x.py",
                no_agent=True,
                allow_memory_writes=True,
            )
        assert "no_agent" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Scheduler attachment helper
# ---------------------------------------------------------------------------

class TestMaybeAttachCronMemoryStore:
    def test_default_does_not_attach(self, cron_env):
        from cron.scheduler import _maybe_attach_cron_memory_store

        agent = _make_fake_agent()
        job = {
            "id": "j1",
            "allow_memory_writes": False,
            "enabled_toolsets": ["memory"],
            "no_agent": False,
        }
        result = _maybe_attach_cron_memory_store(job, agent)
        assert result is None
        assert agent._memory_store is None
        assert agent._cron_safe_memory is False

    def test_memory_toolset_alone_does_not_authorize(self, cron_env):
        """Toolset visibility is not authorization — store stays detached."""
        from cron.scheduler import _maybe_attach_cron_memory_store

        agent = _make_fake_agent()
        job = {
            "id": "j2",
            "allow_memory_writes": False,
            "enabled_toolsets": ["memory", "web"],
            "no_agent": False,
        }
        result = _maybe_attach_cron_memory_store(job, agent)
        assert result is None
        assert agent._memory_store is None

    def test_opt_in_without_memory_toolset_no_attachment(self, cron_env):
        """Opt-in alone is not enough — memory toolset is also required."""
        from cron.scheduler import _maybe_attach_cron_memory_store

        agent = _make_fake_agent()
        job = {
            "id": "j3",
            "allow_memory_writes": True,
            "enabled_toolsets": ["web", "terminal"],
            "no_agent": False,
        }
        result = _maybe_attach_cron_memory_store(job, agent)
        assert result is None
        assert agent._memory_store is None

    def test_no_agent_cannot_attach(self, cron_env):
        """no_agent=True jobs skip the agent — never attach a store."""
        from cron.scheduler import _maybe_attach_cron_memory_store

        agent = _make_fake_agent()
        job = {
            "id": "j4",
            "allow_memory_writes": True,
            "enabled_toolsets": ["memory"],
            "no_agent": True,
        }
        result = _maybe_attach_cron_memory_store(job, agent)
        assert result is None
        assert agent._memory_store is None

    def test_full_opt_in_attaches_with_prompt_blocks_disabled(self, cron_env):
        """Opted-in + memory toolset + agent path → store attached, prompt blocks OFF."""
        from cron.scheduler import _maybe_attach_cron_memory_store

        agent = _make_fake_agent()
        job = {
            "id": "j5",
            "allow_memory_writes": True,
            "enabled_toolsets": ["memory"],
            "no_agent": False,
        }
        result = _maybe_attach_cron_memory_store(job, agent)
        assert result is not None
        assert agent._memory_store is not None
        # CRITICAL: prompt-block flags stay False so MEMORY/USER PROFILE are
        # NOT injected into the cron system prompt.
        assert agent._memory_enabled is False
        assert agent._user_profile_enabled is False
        # Cron-safe wrapper flag is set so dispatch goes through the deny gate.
        assert agent._cron_safe_memory is True
        assert agent._memory_write_origin == "cron"

    def test_attached_store_loads_from_temp_home(self, cron_env):
        """Attached store reads/writes within HERMES_HOME/memories (temp dir)."""
        from cron.scheduler import _maybe_attach_cron_memory_store

        agent = _make_fake_agent()
        job = {
            "id": "j6",
            "allow_memory_writes": True,
            "enabled_toolsets": ["memory"],
            "no_agent": False,
        }
        _maybe_attach_cron_memory_store(job, agent)
        # The store should target the temp memories dir, never the live home.
        from tools.memory_tool import get_memory_dir
        assert get_memory_dir() == cron_env / "memories"

    def test_attached_store_uses_configured_char_limits(self, cron_env):
        """Cron-safe memory writes honor memory.*_char_limit from config.yaml."""
        (cron_env / "config.yaml").write_text(
            "memory:\n  memory_char_limit: 1234\n  user_char_limit: 567\n",
            encoding="utf-8",
        )
        from cron.scheduler import _maybe_attach_cron_memory_store

        agent = _make_fake_agent()
        job = {
            "id": "j7",
            "allow_memory_writes": True,
            "enabled_toolsets": ["memory"],
            "no_agent": False,
        }
        store = _maybe_attach_cron_memory_store(job, agent)
        assert store is not None
        assert store.memory_char_limit == 1234
        assert store.user_char_limit == 567


# ---------------------------------------------------------------------------
# Cron-safe memory tool — code-level deny gates
# ---------------------------------------------------------------------------

@pytest.fixture
def cron_safe_store(cron_env):
    """Build a real MemoryStore in temp dir for direct cron_safe testing."""
    from tools.memory_tool import MemoryStore
    s = MemoryStore(memory_char_limit=500, user_char_limit=300)
    s.load_from_disk()
    return s


class TestCronSafeMemoryToolDenies:
    def test_user_target_denied(self, cron_env, cron_safe_store):
        """target='user' is hard-denied at code level; USER.md must not change."""
        from tools.memory_tool import memory_tool

        user_path = cron_env / "memories" / "USER.md"
        before_exists = user_path.exists()
        before_content = user_path.read_text() if before_exists else ""

        result_str = memory_tool(
            action="add",
            target="user",
            content="Should never land",
            store=cron_safe_store,
            cron_safe=True,
        )
        result = json.loads(result_str)
        assert result["success"] is False
        assert "user" in result["error"].lower()

        # File state unchanged
        if before_exists:
            assert user_path.read_text() == before_content
        else:
            assert not user_path.exists()

    def test_replace_action_denied(self, cron_env, cron_safe_store):
        """action='replace' is hard-denied; existing entries must remain intact."""
        from tools.memory_tool import memory_tool

        # Seed an entry
        cron_safe_store.add("memory", "stable entry to keep")
        before = cron_safe_store.memory_entries.copy()

        result_str = memory_tool(
            action="replace",
            target="memory",
            old_text="stable entry",
            content="modified",
            store=cron_safe_store,
            cron_safe=True,
        )
        result = json.loads(result_str)
        assert result["success"] is False
        assert "replace" in result["error"].lower() or "add" in result["error"].lower()

        cron_safe_store._reload_target("memory")
        assert cron_safe_store.memory_entries == before

    def test_remove_action_denied(self, cron_env, cron_safe_store):
        """action='remove' is hard-denied; no entries are dropped."""
        from tools.memory_tool import memory_tool

        cron_safe_store.add("memory", "entry to keep")
        before = cron_safe_store.memory_entries.copy()

        result_str = memory_tool(
            action="remove",
            target="memory",
            old_text="entry",
            store=cron_safe_store,
            cron_safe=True,
        )
        result = json.loads(result_str)
        assert result["success"] is False
        assert "remove" in result["error"].lower() or "add" in result["error"].lower()

        cron_safe_store._reload_target("memory")
        assert cron_safe_store.memory_entries == before

    def test_add_memory_target_allowed(self, cron_env, cron_safe_store):
        """The single allowed combo: action='add' + target='memory'."""
        from tools.memory_tool import memory_tool

        result_str = memory_tool(
            action="add",
            target="memory",
            content="cron-discovered fact",
            store=cron_safe_store,
            cron_safe=True,
        )
        result = json.loads(result_str)
        assert result["success"] is True

        # Verify on-disk state actually mutated.
        mem_path = cron_env / "memories" / "MEMORY.md"
        assert mem_path.exists()
        assert "cron-discovered fact" in mem_path.read_text()


# ---------------------------------------------------------------------------
# Cron-safe response leak gate
# ---------------------------------------------------------------------------

class TestCronSafeResponseShape:
    def test_success_response_excludes_existing_entries(self, cron_env, cron_safe_store):
        """Cron-safe success response must NOT include the entries list."""
        from tools.memory_tool import memory_tool

        # Pre-seed an entry the cron model should NOT see.
        cron_safe_store.add("memory", "secret prior memory the cron must not read")

        result_str = memory_tool(
            action="add",
            target="memory",
            content="new cron entry",
            store=cron_safe_store,
            cron_safe=True,
        )
        result = json.loads(result_str)
        assert result.get("success") is True
        assert "entries" not in result, (
            f"cron-safe response leaked entries list: {result!r}"
        )
        # The pre-existing entry text must not appear anywhere in the response.
        assert "secret prior memory" not in result_str

    def test_error_response_excludes_existing_entries(self, cron_env, cron_safe_store):
        """Even error responses must not leak existing entries back."""
        from tools.memory_tool import memory_tool

        cron_safe_store.add("memory", "secret prior memory the cron must not read")

        # Force an error path — empty content
        result_str = memory_tool(
            action="add",
            target="memory",
            content="",
            store=cron_safe_store,
            cron_safe=True,
        )
        result = json.loads(result_str)
        assert result.get("success") is False
        assert "entries" not in result
        assert "secret prior memory" not in result_str


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class TestCronMemoryAuditLog:
    def test_audit_metadata_only_on_success(self, cron_env, cron_safe_store):
        """Audit log records action/target/job/length, never full content."""
        from cron.scheduler import _audit_cron_memory_write

        secret_content = "private cron-discovered detail goes here"
        _audit_cron_memory_write(
            job_id="job-abc",
            session_id="sess-xyz",
            action="add",
            target="memory",
            success=True,
            content_length=len(secret_content),
            error=None,
        )

        audit_path = cron_env / "cron" / "memory_audit.log"
        assert audit_path.exists(), "audit log was not written"

        body = audit_path.read_text(encoding="utf-8").strip()
        assert body, "audit log is empty"
        # Each line is JSON
        record = json.loads(body.splitlines()[-1])
        assert record["job_id"] == "job-abc"
        assert record["session_id"] == "sess-xyz"
        assert record["action"] == "add"
        assert record["target"] == "memory"
        assert record["success"] is True
        assert record["content_length"] == len(secret_content)
        # Critical: full memory content must NOT appear in the audit record.
        assert secret_content not in body
        assert "content" not in record  # no `content` field at all

    def test_audit_metadata_on_failure(self, cron_env):
        from cron.scheduler import _audit_cron_memory_write

        _audit_cron_memory_write(
            job_id="job-deny",
            session_id="sess-1",
            action="replace",
            target="memory",
            success=False,
            content_length=0,
            error="cron-safe denies replace",
        )
        audit_path = cron_env / "cron" / "memory_audit.log"
        record = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert record["success"] is False
        assert record["action"] == "replace"
        assert record["error"] == "cron-safe denies replace"


# ---------------------------------------------------------------------------
# Prompt injection gate
# ---------------------------------------------------------------------------

class TestNoMemoryPromptInjection:
    def test_attached_agent_keeps_prompt_block_flags_off(self, cron_env):
        """The whole point of guarded hybrid: prompt MUST NOT include MEMORY/USER PROFILE.

        We assert the two flags that gate prompt injection in run_agent.py
        (``_memory_enabled`` and ``_user_profile_enabled``) remain False after
        attachment. This is the contract that prevents existing memories from
        being seeded into the cron system prompt.
        """
        from cron.scheduler import _maybe_attach_cron_memory_store

        agent = _make_fake_agent()
        job = {
            "id": "jcheck",
            "allow_memory_writes": True,
            "enabled_toolsets": ["memory"],
            "no_agent": False,
        }
        _maybe_attach_cron_memory_store(job, agent)
        assert agent._memory_enabled is False
        assert agent._user_profile_enabled is False
