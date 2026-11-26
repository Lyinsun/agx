#!/usr/bin/env python3
"""Isolated automated test suite for the standalone agx toolset."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGX_BIN = REPO_ROOT / "bin" / "agx"
AGX_WEB_BIN = REPO_ROOT / "bin" / "agx-web"
KABOO_SYNC_BIN = REPO_ROOT / "bin" / "agx-kaboo-sync"
APPEAL_WATCH_BIN = REPO_ROOT / "bin" / "agx-appeal-watch"
STATUSLINE_SH = REPO_ROOT / "config" / "statusline.sh"
INSTALL_SH = REPO_ROOT / "install.sh"


def write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def make_account(
    pool_root: Path,
    name: str,
    *,
    status: str = "ok",
    plan: str = "",
    email: str = "",
    usage_rows: list[tuple[str, str, str, str]] | None = None,
    last_used: int | None = None,
    models_txt: str | None = None,
) -> Path:
    acc = pool_root / "accounts" / name
    tok_dir = acc / "home" / ".gemini" / "antigravity-cli"
    (tok_dir / "log").mkdir(parents=True, exist_ok=True)
    (tok_dir / "conversations").mkdir(parents=True, exist_ok=True)
    (tok_dir / "antigravity-oauth-token").write_text("dummy-token\n", encoding="utf-8")
    (acc / "status").write_text(status + "\n", encoding="utf-8")
    (acc / "email").write_text((email or f"{name}@example.com") + "\n", encoding="utf-8")
    if plan:
        (acc / "plan").write_text(plan + "\n", encoding="utf-8")
    if usage_rows is not None:
        lines = ["\t".join(r) for r in usage_rows]
        (acc / "usage.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if last_used is not None:
        (acc / "last_used").write_text(f"{last_used}\n", encoding="utf-8")
    if models_txt is not None:
        (acc / "models.txt").write_text(models_txt, encoding="utf-8")
    return acc


class AgxCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agx_test_"))
        self.real_home = self.tmp / "home"
        self.real_home.mkdir()
        self.pool = self.tmp / "pool"
        self.pool.mkdir()
        self.fake_agy = self.tmp / "fake_agy"
        write_executable(
            self.fake_agy,
            """#!/usr/bin/env bash
set -e
if [ "${1:-}" = "models" ]; then
    if [ -n "${FAKE_MODELS_FILE:-}" ] && [ -f "$FAKE_MODELS_FILE" ]; then
        cat "$FAKE_MODELS_FILE"
    else
        printf 'gemini-4-neon-high\\ngemini-4-neon-medium\\nclaude-sonnet-5-5-high\\n'
    fi
    exit 0
fi
if [ -n "${FAKE_LOG_LINE:-}" ]; then
    logdir="$HOME/.gemini/antigravity-cli/log"
    mkdir -p "$logdir"
    printf '%s\\n' "$FAKE_LOG_LINE" > "$logdir/cli-20990101_000001.log"
fi
echo "FAKE_AGY_ARGS: $*"
""",
        )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def base_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.real_home),
            "AGYX_REAL_HOME": str(self.real_home),
            "AGYX_ROOT": str(self.pool),
            "AGYX_AGY": str(self.fake_agy),
            "AGYX_REFRESH_MIN": "0",
            "AGYX_PROXY": "keep",
        }
        if extra:
            env.update(extra)
        return env

    def run_agx(self, args: list[str], extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(AGX_BIN), *args],
            env=self.base_env(extra_env),
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_weighted_rotation_and_lru(self) -> None:
        future = "2099-01-01T00:00:00Z"
        make_account(
            self.pool,
            "pro1",
            plan="Google AI Pro",
            usage_rows=[
                ("Gemini Models", "Five Hour", "100%", future),
                ("Gemini Models", "Weekly", "100%", future),
            ],
            last_used=100,
        )
        make_account(
            self.pool,
            "ultra_low",
            plan="Google AI Ultra",
            usage_rows=[
                ("Gemini Models", "Five Hour", "80%", future),
                ("Gemini Models", "Weekly", "80%", future),
            ],
            last_used=50,
        )
        make_account(
            self.pool,
            "ultra_high",
            plan="Google AI Ultra",
            usage_rows=[
                ("Gemini Models", "Five Hour", "98%", future),
                ("Gemini Models", "Weekly", "100%", future),
            ],
            last_used=200,
        )
        (self.pool / "order").write_text("pro1\nultra_low\nultra_high\n", encoding="utf-8")

        # Default spread: ultra_high (20.0 * 0.98 = 19.6) beats ultra_low (16.0) and pro1 (1.00)
        r = self.run_agx(["pick", "gemini-4-neon-high"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ultra_high")

        # --explain prints table header and winner with 20.0 Ultra weight
        r_exp = self.run_agx(["pick", "--explain", "gemini-4-neon-high"])
        self.assertEqual(r_exp.returncode, 0, r_exp.stderr)
        self.assertIn("ACCOUNT", r_exp.stderr)
        self.assertIn("20.0", r_exp.stderr)
        self.assertEqual(r_exp.stdout.strip(), "ultra_high")

        # Tie-breaking via LRU when within relative 5% AVAIL bucket (98% vs 97% Ultra)
        make_account(
            self.pool,
            "ultra_tie",
            plan="Google AI Ultra",
            usage_rows=[
                ("Gemini Models", "Five Hour", "97%", future),
                ("Gemini Models", "Weekly", "100%", future),
            ],
            last_used=10,
        )
        r_lru = self.run_agx(["pick", "gemini-4-neon-high"])
        self.assertEqual(r_lru.stdout.strip(), "ultra_tie")

        # Ultra at 10% remaining (20.0 * 0.10 = 2.0) still beats idle Pro at 100% (1.0),
        # while Ultra at 4% remaining (0.8) yields to idle Pro (1.0)
        make_account(
            self.pool,
            "starter_acct",
            plan="Antigravity Starter Quota",
            usage_rows=[("Gemini Models", "Five Hour", "100%", future)],
        )
        r_low_u = self.run_agx(
            ["pick", "gemini-4-neon-high"],
            {"AGYX_EXCLUDE_ACCOUNTS": "ultra_high,ultra_tie,ultra_low"},
        )
        self.assertEqual(r_low_u.stdout.strip(), "pro1")

        # --prefer-plan pro overrides Ultra
        r_pro = self.run_agx(["pick", "--prefer-plan", "pro", "gemini-4-neon-high"])
        self.assertEqual(r_pro.stdout.strip(), "pro1")

        # AGYX_PICK_STRATEGY=order falls back to order file
        r_ord = self.run_agx(["pick", "gemini-4-neon-high"], {"AGYX_PICK_STRATEGY": "order"})
        self.assertEqual(r_ord.stdout.strip(), "pro1")

    def test_models_sync_and_model_filter(self) -> None:
        future = "2099-01-01T00:00:00Z"
        make_account(
            self.pool,
            "acct_ok",
            plan="Google AI Ultra",
            usage_rows=[("Gemini Models", "Five Hour", "100%", future)],
        )
        make_account(
            self.pool,
            "acct_bad",
            status="bad:TOS_VIOLATION:2026-11-26T00:00:00+0800",
            plan="Google AI Ultra",
        )
        r = self.run_agx(["models-sync", "all"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.pool / "accounts" / "acct_ok" / "models.txt").is_file())
        self.assertFalse((self.pool / "accounts" / "acct_bad" / "models.txt").exists())

        # Create a Pro account with opus and confirm ultra without opus is skipped
        make_account(
            self.pool,
            "acct_pro_opus",
            plan="Google AI Pro",
            usage_rows=[("Claude and GPT models", "Five Hour", "100%", future)],
            models_txt="gemini-4-neon-high\nclaude-opus-5-5-high\n",
        )
        (self.pool / "accounts" / "acct_ok" / "usage.tsv").write_text(
            f"Claude and GPT models\tFive Hour\t100%\t{future}\n", encoding="utf-8"
        )
        r_pick = self.run_agx(["pick", "claude-opus-5-5-high"])
        self.assertEqual(r_pick.returncode, 0, r_pick.stderr)
        self.assertEqual(r_pick.stdout.strip(), "acct_pro_opus")

    def test_scan_logs_new_only_and_per_group_cooldown(self) -> None:
        future = "2099-01-01T00:00:00Z"
        acc = make_account(
            self.pool,
            "acct1",
            plan="Google AI Pro",
            usage_rows=[
                ("Gemini Models", "Five Hour", "100%", future),
                ("Claude and GPT models", "Five Hour", "100%", future),
            ],
        )
        log_dir = acc / "home" / ".gemini" / "antigravity-cli" / "log"
        # Old concurrent log containing background ACL 429 and RESOURCE_EXHAUSTED must be ignored
        (log_dir / "cli-20260101_000001.log").write_text(
            "I0101 00:00:01 v1internal:writeTrajectoryAcls RESOURCE_EXHAUSTED (code 429)\n",
            encoding="utf-8",
        )
        r_clean = self.run_agx(["-a", "acct1", "-p", "hi"])
        self.assertEqual(r_clean.returncode, 0, r_clean.stderr)
        self.assertEqual((acc / "status").read_text(encoding="utf-8").strip(), "ok")
        self.assertFalse((acc / "last_429.claude").exists())

        # New E-level Third-party quota reached sets exhausted:claude + last_429.claude, Gemini still pickable
        r_429 = self.run_agx(
            ["-a", "acct1", "--model", "claude-opus-5-5-high", "-p", "hi"],
            {"FAKE_LOG_LINE": "E1126 10:00:00 session.go:100 Third-party quota reached. Please switch to Gemini models"},
        )
        self.assertEqual(r_429.returncode, 0, r_429.stderr)
        st = (acc / "status").read_text(encoding="utf-8").strip()
        self.assertTrue(st.startswith("exhausted:claude:"), st)
        self.assertTrue((acc / "last_429.claude").is_file())
        self.assertFalse((acc / "last_429.gemini").exists())

        # Gemini pick still selects acct1; Claude pick rejects acct1
        self.assertEqual(self.run_agx(["pick", "gemini-4-neon-high"]).stdout.strip(), "acct1")
        self.assertNotEqual(self.run_agx(["pick", "claude-opus-5-5-high"]).returncode, 0)

        # Text-only ToS violation triggers bad: and PAUSED
        (log_dir / "cli-20990101_000001.log").unlink()
        (acc / "status").write_text("ok\n", encoding="utf-8")
        self.run_agx(
            ["-a", "acct1", "-p", "hi"],
            {"FAKE_LOG_LINE": "E1126 10:01:00 Account disabled due to violation of Terms of Service"},
        )
        self.assertTrue((self.pool / "PAUSED").is_file())
        self.assertTrue((acc / "status").read_text(encoding="utf-8").startswith("bad:"))

    def test_handoff_wal_backup_and_validation(self) -> None:
        future = "2099-01-01T00:00:00Z"
        src = make_account(
            self.pool,
            "src_acct",
            plan="Google AI Pro",
            usage_rows=[("Gemini Models", "Five Hour", "1%", future)],
        )
        dst = make_account(
            self.pool,
            "dst_acct",
            plan="Google AI Ultra",
            usage_rows=[("Gemini Models", "Five Hour", "100%", future)],
        )
        cid = "11111111-2222-3333-4444-555555555555"
        src_root = src / "home" / ".gemini" / "antigravity-cli"
        db_path = src_root / "conversations" / f"{cid}.db"

        # Keep an active WAL writer open to verify SQLite backup() captures WAL rows safely
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);")
        conn.execute("INSERT INTO kv VALUES ('secret', '73941');")
        conn.commit()
        try:
            (src_root / "brain" / cid).mkdir(parents=True)
            (src_root / "brain" / cid / "note.md").write_text("handoff ok\n", encoding="utf-8")
            (src_root / "annotations").mkdir(parents=True)
            (src_root / "annotations" / f"{cid}.pbtxt").write_text("tag: 1\n", encoding="utf-8")
            (src_root / "settings.json").write_text(
                json.dumps({"trustedWorkspaces": [os.getcwd()]}), encoding="utf-8"
            )

            # Passthrough -p "hello world" must NOT be swallowed as dst account
            r = self.run_agx(["handoff", cid, "--no-run", "-p", "hello world"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("agx -a dst_acct --conversation", r.stdout)
            self.assertIn("hello\\ world", r.stdout)

            dst_root = dst / "home" / ".gemini" / "antigravity-cli"
            dst_db = dst_root / "conversations" / f"{cid}.db"
            self.assertTrue(dst_db.is_file())
            dconn = sqlite3.connect(str(dst_db))
            try:
                val = dconn.execute("SELECT v FROM kv WHERE k='secret'").fetchone()[0]
            finally:
                dconn.close()
            self.assertEqual(val, "73941")
            self.assertEqual((dst_root / "brain" / cid / "note.md").read_text(encoding="utf-8"), "handoff ok\n")
            self.assertEqual((dst_root / "annotations" / f"{cid}.pbtxt").read_text(encoding="utf-8"), "tag: 1\n")
            dst_cfg = json.loads((dst_root / "settings.json").read_text(encoding="utf-8"))
            self.assertIn(os.getcwd(), dst_cfg.get("trustedWorkspaces", []))
        finally:
            conn.close()

        # Path traversal & invalid account names must be rejected
        for bad_args in (
            ["handoff", "../escape", "dst_acct", "--no-run"],
            ["handoff", cid, "dst_acct/", "--no-run"],
            ["handoff", cid, "--from", "../src_acct", "--no-run"],
        ):
            rb = self.run_agx(bad_args)
            self.assertNotEqual(rb.returncode, 0, f"should reject {bad_args}")

    def test_config_env_loading(self) -> None:
        make_account(self.pool, "acct1")
        cfg_dir = self.real_home / ".config" / "agx"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.env").write_text(
            'AGYX_PROXY="http://127.0.0.1:9999"\nAGYX_NO_PROXY="localhost,127.0.0.1"\n',
            encoding="utf-8",
        )
        # Without explicit AGYX_PROXY, config.env takes effect
        env_no_proxy = self.base_env()
        env_no_proxy.pop("AGYX_PROXY", None)
        r1 = subprocess.run(
            ["bash", str(AGX_BIN), "env", "acct1"],
            env=env_no_proxy,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn("http_proxy=http://127.0.0.1:9999", r1.stdout)
        self.assertIn("no_proxy=localhost,127.0.0.1", r1.stdout)

        # Explicit env var overrides config.env
        r2 = self.run_agx(["env", "acct1"], {"AGYX_PROXY": "keep"})
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertNotIn("http_proxy=", r2.stdout)


class AgxCompanionToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agx_tools_test_"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_agx_web_bind_guard_and_token_auth(self) -> None:
        real_home = self.tmp / "home"
        real_home.mkdir()
        pool = self.tmp / "pool"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(real_home),
            "AGYX_REAL_HOME": str(real_home),
            "AGYX_ROOT": str(pool),
            "AGX_WEB_HOST": "0.0.0.0",
        }
        r_refuse = subprocess.run(
            ["python3", str(AGX_WEB_BIN)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(r_refuse.returncode, 0)

        # Pick an ephemeral localhost port and verify token auth
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        env["AGX_WEB_HOST"] = "127.0.0.1"
        env["AGX_WEB_PORT"] = str(port)
        proc = subprocess.Popen(
            ["python3", str(AGX_WEB_BIN)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            tok_file = pool / "web.token"
            for _ in range(40):
                if tok_file.is_file():
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        pass
                time.sleep(0.1)
            self.assertTrue(tok_file.is_file(), "web.token not created")
            token = tok_file.read_text(encoding="utf-8").strip()

            # Without token -> 401
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/pool", timeout=5)
            self.assertEqual(ctx.exception.code, 401)

            # With X-AGX-Token -> 200 JSON
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/pool",
                headers={"X-AGX-Token": token},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("accounts", data)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_kaboo_sync(self) -> None:
        real_home = self.tmp / "home"
        pool = self.tmp / "pool"
        acc = make_account(pool, "acct1")
        conv = acc / "home" / ".gemini" / "antigravity-cli" / "conversations"
        (conv / "aaa.db").write_text("db", encoding="utf-8")
        (conv / "aaa.db-wal").write_text("wal", encoding="utf-8")

        target = real_home / ".gemini" / "antigravity-cli" / "conversations"
        target.mkdir(parents=True)
        # Existing real file must NOT be overwritten
        (target / "keep_real.db").write_text("real", encoding="utf-8")
        # Stale dangling pool symlink must be removed
        (target / "stale.db").symlink_to(pool / "accounts" / "old" / "home" / ".gemini" / "antigravity-cli" / "conversations" / "stale.db")

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(real_home),
            "AGYX_REAL_HOME": str(real_home),
            "AGYX_ROOT": str(pool),
        }
        r = subprocess.run(["python3", str(KABOO_SYNC_BIN)], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((target / "aaa.db").is_symlink())
        self.assertTrue((target / "aaa.db-wal").is_symlink())
        self.assertFalse((target / "stale.db").exists() or (target / "stale.db").is_symlink())
        self.assertFalse((target / "keep_real.db").is_symlink())
        self.assertEqual((target / "keep_real.db").read_text(encoding="utf-8"), "real")

    def test_statusline_tps_and_degradation(self) -> None:
        acct_home = self.tmp / "acct_home"
        cid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        # Place transcript in antigravity-cli/brain while payload passes antigravity/brain (mimics real agy bug)
        t_dir = acct_home / ".gemini" / "antigravity-cli" / "brain" / cid / ".system_generated" / "logs"
        t_dir.mkdir(parents=True)
        t_real = t_dir / "transcript.jsonl"
        t_real.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "PLANNER_RESPONSE",
                            "created_at": "2026-11-26T10:00:00.000Z",
                            "output_tokens": 200,
                        }
                    ),
                    json.dumps(
                        {
                            "type": "GENERIC",
                            "source": "MODEL",
                            "created_at": "2026-11-26T10:00:04.000Z",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        t_passed = acct_home / ".gemini" / "antigravity" / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        payload = {
            "model": {"display_name": "Gemini 4 Neon"},
            "workspace": {"current_dir": str(self.tmp)},
            "context_window": {
                "used_percentage": 25,
                "current_usage": {"input_tokens": 5000, "cache_read_input_tokens": 1000, "output_tokens": 200},
                "context_window_size": 1000000,
            },
            "transcript_path": str(t_passed),
            "antigravity": {"plan_tier": "Google AI Ultra"},
        }
        r = subprocess.run(
            ["bash", str(STATUSLINE_SH)],
            input=json.dumps(payload),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(acct_home)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Gemini 4 Neon", r.stdout)
        self.assertIn("TPS", r.stdout)

        # Corrupt transcript degrades silently
        t_real.write_text("{not-json\n", encoding="utf-8")
        r_bad = subprocess.run(
            ["bash", str(STATUSLINE_SH)],
            input=json.dumps(payload),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(acct_home)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r_bad.returncode, 0, r_bad.stderr)
        self.assertIn("Gemini 4 Neon", r_bad.stdout)
        self.assertNotIn("TPS", r_bad.stdout)

    def test_install_and_uninstall_isolated(self) -> None:
        fake_home = self.tmp / "install_home"
        fake_home.mkdir()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(fake_home)}

        r_in = subprocess.run(["bash", str(INSTALL_SH), "--all"], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(r_in.returncode, 0, r_in.stderr)
        for rel in (
            ".local/bin/agx",
            ".local/bin/agyx",
            ".local/bin/agx-web",
            ".local/bin/agx-appeal-watch",
            ".local/bin/agx-kaboo-sync",
            ".gemini/config/statusline.sh",
            ".gemini/config/title.sh",
        ):
            p = fake_home / rel
            self.assertTrue(p.is_symlink(), f"missing symlink {rel}")
        self.assertTrue((fake_home / ".config" / "agx" / "config.env").is_file())

        r_un = subprocess.run(["bash", str(INSTALL_SH), "--uninstall"], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(r_un.returncode, 0, r_un.stderr)
        for rel in (".local/bin/agx", ".local/bin/agyx", ".gemini/config/statusline.sh"):
            p = fake_home / rel
            self.assertFalse(p.exists() or p.is_symlink(), f"not removed: {rel}")
        # Uninstall keeps user config.env intact
        self.assertTrue((fake_home / ".config" / "agx" / "config.env").is_file())

    def test_no_personal_or_internal_leaks(self) -> None:
        import re

        forbidden_patterns = [
            (r"(?<![A-Za-z0-9_/-])/home/[a-zA-Z][a-zA-Z0-9._-]*", "hardcoded /home/<user> path"),
            (r"(?<![A-Za-z0-9_/-])/Users/[a-zA-Z][a-zA-Z0-9._-]*", "hardcoded /Users/<user> path"),
            (r"\bbyted(ance)?\.(org|net|com)\b", "internal corporate domain"),
            (r"\bdevbox[0-9]+\b", "internal host alias"),
            (r"\b[0-9]{18,20}\b", "hardcoded numeric feed/chat ID"),
        ]
        hits: list[str] = []
        for path in sorted(REPO_ROOT.rglob("*")):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pat, desc in forbidden_patterns:
                for m in re.finditer(pat, text, flags=re.IGNORECASE):
                    hits.append(f"{path.relative_to(REPO_ROOT)}: {desc} ({m.group(0)})")
        self.assertEqual(hits, [], f"Sensitive/internal patterns found: {hits}")


if __name__ == "__main__":
    unittest.main()
