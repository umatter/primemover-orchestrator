import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from pmspec import cli as cli_module
from pmspec.cli import cli
from pmspec.lock.api import ApiError
from pmspec.lock.io import dump_lock
from pmspec.lock.secrets import SecretStore
from pmspec.lock.upload import UploadResult

from .test_lock_io import make_lock


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    p = tmp_path / "study.lock.yaml"
    p.write_text(dump_lock(make_lock()))
    return p


@pytest.fixture
def secrets_file(tmp_path: Path) -> Path:
    p = tmp_path / "secrets.toml"
    p.write_text('[oxylabs.a]\nusername = "u"\npassword = "testpass"\n')
    p.chmod(0o600)
    return p


@pytest.fixture
def fake_upload(monkeypatch: pytest.MonkeyPatch):
    calls = {}

    def _upload_lock(lock_text, *, session, store, options):
        calls["options"] = options
        calls["has_store"] = store is not None
        return UploadResult(
            181,
            "Study",
            1,
            1,
            "f" * 64,
            "2026-10-01T10:00:00+02:00",
            options.mode,
            None,
            "2026-09-11T10:00:00+02:00 exp=181 mode=real lock=ffff commit=a2024c9 api=x",
        )

    class _Verify:
        def __init__(self, ok):
            self.ok = ok
            self.bound = ok
            self.differences = []

        def render(self):
            return "BOUND" if self.ok else "UNBOUND"

    monkeypatch.setattr(cli_module, "upload_lock", _upload_lock)
    monkeypatch.setattr(
        cli_module, "verify_lock", lambda *a, **k: _Verify(calls.get("verify_ok", True))
    )
    monkeypatch.setattr(cli_module, "_session_from_env", lambda: object())
    return calls


def test_upload_requires_mode(lock_file):
    out = CliRunner().invoke(cli, ["upload", str(lock_file)])
    assert out.exit_code != 0 and "--mode" in out.output


def test_upload_dry_run_checks_secrets_and_does_not_upload(lock_file, secrets_file, fake_upload):
    out = CliRunner().invoke(
        cli,
        ["upload", str(lock_file), "--mode", "real", "--dry-run", "--secrets", str(secrets_file)],
    )
    assert out.exit_code == 0, out.output
    assert "oxylabs.a.username" in out.output and "Dry run" in out.output
    assert "options" not in fake_upload


def test_upload_dry_run_reports_missing_secret(lock_file, tmp_path, fake_upload):
    empty = tmp_path / "e.toml"
    empty.write_text("")
    empty.chmod(0o600)
    out = CliRunner().invoke(
        cli, ["upload", str(lock_file), "--mode", "real", "--dry-run", "--secrets", str(empty)]
    )
    assert out.exit_code == 1 and "MISSING" in out.output and "testpass" not in out.output


def test_upload_writes_record_and_verifies(lock_file, secrets_file, fake_upload, tmp_path):
    record = tmp_path / "ledger.txt"
    out = CliRunner().invoke(
        cli,
        [
            "upload",
            str(lock_file),
            "--mode",
            "test",
            "--secrets",
            str(secrets_file),
            "--record",
            str(record),
        ],
    )
    assert out.exit_code == 0, out.output
    assert fake_upload["options"].mode == "test" and fake_upload["has_store"]
    assert record.read_text().strip().endswith("api=x")
    assert "BOUND" in out.output and "exp=181" in out.output and "api=x" in out.output


def test_upload_exits_1_when_post_upload_verify_fails(lock_file, secrets_file, fake_upload):
    fake_upload["verify_ok"] = False
    out = CliRunner().invoke(
        cli, ["upload", str(lock_file), "--mode", "test", "--secrets", str(secrets_file)]
    )
    assert out.exit_code == 1 and "UNBOUND" in out.output and "exp=181" in out.output


def test_upload_keeps_record_when_verify_raises(
    lock_file, secrets_file, fake_upload, tmp_path, monkeypatch
):
    record = tmp_path / "ledger.txt"

    def _raise(*a, **k):
        raise ApiError("GET x: HTTP 500", status=500, detail="boom")

    monkeypatch.setattr(cli_module, "verify_lock", _raise)
    out = CliRunner().invoke(
        cli,
        [
            "upload",
            str(lock_file),
            "--mode",
            "test",
            "--secrets",
            str(secrets_file),
            "--record",
            str(record),
        ],
    )
    assert out.exit_code == 1
    assert "Verify failed after upload" in out.output
    assert record.read_text().strip().endswith("api=x")


def test_upload_compiles_a_spec_first_and_says_to_commit(tmp_path, secrets_file, fake_upload):
    spec = tmp_path / "s.yaml"
    spec.write_text(
        "version: '1.0'\nexperiment: {name: S}\ncrawlers:\n  - name: b\n    session:\n      start: '+1m'\n      workflow:\n        - wait: 1s\n"
    )
    out = CliRunner().invoke(
        cli, ["upload", str(spec), "--mode", "test", "--seed", "1", "--secrets", str(secrets_file)]
    )
    assert out.exit_code == 0, out.output
    assert (tmp_path / "s.lock.yaml").exists() and "commit" in out.output.lower()


def test_verify_command(lock_file, fake_upload):
    out = CliRunner().invoke(cli, ["verify", "7", str(lock_file)])
    assert out.exit_code == 0 and "BOUND" in out.output
    fake_upload["verify_ok"] = False
    out = CliRunner().invoke(cli, ["verify", "7", str(lock_file)])
    assert out.exit_code == 1


def test_secrets_check(lock_file, secrets_file, tmp_path):
    out = CliRunner().invoke(
        cli, ["secrets", "check", str(lock_file), "--secrets", str(secrets_file)]
    )
    assert (
        out.exit_code == 0 and "oxylabs.a.password" in out.output and "testpass" not in out.output
    )
    (tmp_path / "secrets.toml").write_text("[oxylabs.a]\nusername = 'u'\n")
    out = CliRunner().invoke(
        cli, ["secrets", "check", str(lock_file), "--secrets", str(secrets_file)]
    )
    assert out.exit_code == 1 and "MISSING" in out.output


def test_secrets_check_rejects_a_corrupt_lock(tmp_path):
    bad = tmp_path / "bad.lock.yaml"
    bad.write_text("pmspec_lock: 1\nprovenance: {}\n")
    out = CliRunner().invoke(cli, ["secrets", "check", str(bad)])
    assert out.exit_code == 1
    assert "Lock invalid" in out.output
    assert "Traceback" not in out.output


def test_secrets_init_creates_skeleton_with_mode_600(lock_file, tmp_path):
    target = tmp_path / "new-secrets.toml"
    out = CliRunner().invoke(cli, ["secrets", "init", str(lock_file), "--secrets", str(target)])
    assert out.exit_code == 0, out.output
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert SecretStore.load(target).data == {"oxylabs": {"a": {"username": "", "password": ""}}}
    assert "oxylabs.a.username" in out.output
    assert "oxylabs.a.password" in out.output
    assert str(target) in out.output


def test_secrets_init_refuses_to_overwrite_without_force(lock_file, tmp_path):
    target = tmp_path / "secrets.toml"
    original = '[oxylabs.a]\nusername = "u"\n'
    target.write_text(original)
    target.chmod(0o600)
    out = CliRunner().invoke(cli, ["secrets", "init", str(lock_file), "--secrets", str(target)])
    assert out.exit_code == 1
    assert "--force" in out.output
    assert target.read_text() == original


def test_secrets_init_force_merges_keeping_existing_values(lock_file, tmp_path):
    target = tmp_path / "secrets.toml"
    target.write_text('[oxylabs.a]\nusername = "keepme"\nother = "x"\n')
    target.chmod(0o600)
    out = CliRunner().invoke(
        cli, ["secrets", "init", str(lock_file), "--secrets", str(target), "--force"]
    )
    assert out.exit_code == 0, out.output
    data = SecretStore.load(target).data
    assert data["oxylabs"]["a"]["username"] == "keepme"
    assert data["oxylabs"]["a"]["other"] == "x"
    assert data["oxylabs"]["a"]["password"] == ""
    assert "oxylabs.a.password" in out.output
    assert "oxylabs.a.username" in out.output
    assert "keepme" not in out.output
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_secrets_init_force_on_an_insecure_existing_file_is_refused(lock_file, tmp_path):
    target = tmp_path / "secrets.toml"
    target.write_text('[oxylabs.a]\nusername = "keepme"\n')
    target.chmod(0o644)
    out = CliRunner().invoke(
        cli, ["secrets", "init", str(lock_file), "--secrets", str(target), "--force"]
    )
    assert out.exit_code == 1
    assert "chmod 600" in out.output
    assert target.read_text() == '[oxylabs.a]\nusername = "keepme"\n'


def test_secrets_init_then_check_reports_every_added_key_missing(lock_file, tmp_path):
    target = tmp_path / "secrets.toml"
    CliRunner().invoke(cli, ["secrets", "init", str(lock_file), "--secrets", str(target)])
    out = CliRunner().invoke(cli, ["secrets", "check", str(lock_file), "--secrets", str(target)])
    assert out.exit_code == 1
    assert out.output.count("MISSING") == 2


def test_secrets_init_force_refuses_a_leaf_table_collision(lock_file, tmp_path):
    """The store already has `oxylabs.a` as a plain string; the lock needs it to be a
    table (`oxylabs.a.username`/`.password`). Turning it into `{}` would destroy the
    existing secret, so this must refuse rather than silently overwrite it."""
    target = tmp_path / "secrets.toml"
    original = '[oxylabs]\na = "hunter2"\n'
    target.write_text(original)
    target.chmod(0o600)
    out = CliRunner().invoke(
        cli, ["secrets", "init", str(lock_file), "--secrets", str(target), "--force"]
    )
    assert out.exit_code == 1
    assert "oxylabs.a" in out.output
    assert "hunter2" not in out.output
    assert target.read_text() == original


def test_secrets_init_force_refuses_a_non_string_existing_leaf(lock_file, tmp_path):
    target = tmp_path / "secrets.toml"
    original = "[oxylabs.a]\nusername = 5\n"
    target.write_text(original)
    target.chmod(0o600)
    out = CliRunner().invoke(
        cli, ["secrets", "init", str(lock_file), "--secrets", str(target), "--force"]
    )
    assert out.exit_code == 1
    assert "not a string" in out.output
    assert target.read_text() == original


def test_secrets_init_force_writes_through_a_symlink(lock_file, tmp_path):
    """`~/.config/primemover/secrets.toml` is often a symlink into a dotfiles or
    password-store checkout. The atomic rename must land on the symlink's target,
    not replace the link itself with a regular file (which would silently detach
    the store from wherever the user keeps it)."""
    real_dir = tmp_path / "store"
    real_dir.mkdir()
    real = real_dir / "real.toml"
    real.write_text('[oxylabs.a]\nusername = "keepme"\n')
    real.chmod(0o600)
    link = tmp_path / "secrets.toml"
    link.symlink_to(real)
    out = CliRunner().invoke(
        cli, ["secrets", "init", str(lock_file), "--secrets", str(link), "--force"]
    )
    assert out.exit_code == 0, out.output
    assert link.is_symlink() and link.resolve() == real.resolve()
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    data = SecretStore.load(real).data
    assert data["oxylabs"]["a"] == {"username": "keepme", "password": ""}
    assert not (tmp_path / "secrets.toml.tmp").exists()
    assert not (real_dir / "real.toml.tmp").exists()


def test_secrets_init_creates_the_target_through_a_dangling_symlink(lock_file, tmp_path):
    """A symlink whose target does not exist yet (fresh dotfiles checkout): the
    file is created at the target and the link is kept."""
    real = tmp_path / "store" / "real.toml"
    real.parent.mkdir()
    link = tmp_path / "secrets.toml"
    link.symlink_to(real)
    out = CliRunner().invoke(cli, ["secrets", "init", str(lock_file), "--secrets", str(link)])
    assert out.exit_code == 0, out.output
    assert link.is_symlink() and real.is_file()
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert SecretStore.load(link).data == {"oxylabs": {"a": {"username": "", "password": ""}}}


def test_secrets_init_recovers_from_a_stale_tmp_file(lock_file, tmp_path):
    """A `<name>.tmp` left behind by a killed earlier run is ours by naming
    convention; the next init must not die on it with a raw FileExistsError."""
    target = tmp_path / "secrets.toml"
    stale = tmp_path / "secrets.toml.tmp"
    stale.write_text("half-written garbage")
    out = CliRunner().invoke(cli, ["secrets", "init", str(lock_file), "--secrets", str(target)])
    assert out.exit_code == 0, out.output
    assert not stale.exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert SecretStore.load(target).data == {"oxylabs": {"a": {"username": "", "password": ""}}}


def test_secrets_init_force_refuses_a_table_at_a_leaf_path(lock_file, tmp_path):
    """The store has `oxylabs.a.username` as a *table*; `secrets check` reports the
    key MISSING (not a string), so init must not report it "already present" and
    walk away -- nor rewrite the table. It is a conflict, like the reverse case."""
    target = tmp_path / "secrets.toml"
    original = '[oxylabs.a]\npassword = "testpass"\n[oxylabs.a.username]\nx = "y"\n'
    target.write_text(original)
    target.chmod(0o600)
    out = CliRunner().invoke(
        cli, ["secrets", "init", str(lock_file), "--secrets", str(target), "--force"]
    )
    assert out.exit_code == 1, out.output
    assert "oxylabs.a.username" in out.output
    assert "already present" not in out.output
    assert "testpass" not in out.output
    assert target.read_text() == original


def test_secrets_init_force_round_trips_control_characters(lock_file, tmp_path):
    """A value containing a raw newline or a NUL byte must come back identical after
    --force adds an unrelated key -- proving the writer escapes rather than emits a
    literal control character (which tomllib cannot parse back at all)."""
    target = tmp_path / "secrets.toml"
    target.write_text('[oxylabs.a]\nusername = "line1\\nline2"\nother = "\\u0000tail"\n')
    target.chmod(0o600)
    out = CliRunner().invoke(
        cli, ["secrets", "init", str(lock_file), "--secrets", str(target), "--force"]
    )
    assert out.exit_code == 0, out.output
    data = SecretStore.load(target).data
    assert data["oxylabs"]["a"]["username"] == "line1\nline2"
    assert data["oxylabs"]["a"]["other"] == "\x00tail"
    assert data["oxylabs"]["a"]["password"] == ""


def test_toml_escape_round_trips_every_control_character(tmp_path):
    from pmspec.cli import _toml_escape

    original = "".join(chr(cp) for cp in [*range(0x20), 0x7F]) + '"\\end'
    p = tmp_path / "s.toml"
    p.write_text(f'k = "{_toml_escape(original)}"\n')
    p.chmod(0o600)
    assert SecretStore.load(p).data["k"] == original


def test_secrets_init_force_quotes_a_key_that_is_not_bare(lock_file, tmp_path):
    target = tmp_path / "secrets.toml"
    target.write_text('[oxylabs]\n"user.name" = "u"\n')
    target.chmod(0o600)
    out = CliRunner().invoke(
        cli, ["secrets", "init", str(lock_file), "--secrets", str(target), "--force"]
    )
    assert out.exit_code == 0, out.output
    data = SecretStore.load(target).data
    assert data["oxylabs"]["user.name"] == "u"
    assert data["oxylabs"]["a"]["username"] == ""
    assert data["oxylabs"]["a"]["password"] == ""
    assert '"user.name" = "u"' in target.read_text()


def test_secrets_check_rejects_invalid_toml(lock_file, tmp_path):
    target = tmp_path / "secrets.toml"
    target.write_text("this = [is not valid")
    target.chmod(0o600)
    out = CliRunner().invoke(cli, ["secrets", "check", str(lock_file), "--secrets", str(target)])
    assert out.exit_code == 1
    assert "not valid TOML" in out.output
    assert "Traceback" not in out.output


def test_secrets_init_force_rejects_invalid_toml(lock_file, tmp_path):
    target = tmp_path / "secrets.toml"
    original = "this = [is not valid"
    target.write_text(original)
    target.chmod(0o600)
    out = CliRunner().invoke(
        cli, ["secrets", "init", str(lock_file), "--secrets", str(target), "--force"]
    )
    assert out.exit_code == 1
    assert "not valid TOML" in out.output
    assert target.read_text() == original


def test_secrets_init_with_no_placeholders_writes_nothing(tmp_path):
    text = dump_lock(make_lock())
    text = text.replace("${secret:oxylabs.a.username}", "literal-user")
    text = text.replace("${secret:oxylabs.a.password}", "literal-pass")
    lock_path = tmp_path / "no-secrets.lock.yaml"
    lock_path.write_text(text)
    target = tmp_path / "secrets.toml"
    out = CliRunner().invoke(cli, ["secrets", "init", str(lock_path), "--secrets", str(target)])
    assert out.exit_code == 0, out.output
    assert "none referenced" in out.output
    assert not target.exists()


def test_upload_reports_an_anchor_error(lock_file, secrets_file, fake_upload, monkeypatch):
    from pmspec.lock.anchor import AnchorError

    def refusing(lock_text, *, session, store, options):
        raise AnchorError("cannot parse start time 'x'")

    monkeypatch.setattr(cli_module, "upload_lock", refusing)
    out = CliRunner().invoke(
        cli, ["upload", str(lock_file), "--mode", "test", "--secrets", str(secrets_file)]
    )
    assert out.exit_code == 1 and "Refused: cannot parse start time" in out.output


def test_generate_reports_an_anchor_error(tmp_path, monkeypatch):
    from pmspec.lock.anchor import AnchorError

    lock = make_lock(schedule={"anchor": "not-a-time"})
    p = tmp_path / "bad-anchor.lock.yaml"
    p.write_text(dump_lock(lock))
    out = CliRunner().invoke(cli, ["generate", str(p)])
    assert out.exit_code == 1, out.output
    assert "cannot parse start time" in out.output
    assert not isinstance(out.exception, AnchorError)


# --- a runs lock -----------------------------------------------------------


@pytest.fixture
def runs_lock_file(tmp_path: Path) -> Path:
    from .test_lock_runs_upload import make_runs_lock

    p = tmp_path / "day.lock.yaml"
    p.write_text(dump_lock(make_runs_lock()))
    return p


@pytest.fixture
def runs_session(monkeypatch: pytest.MonkeyPatch):
    from .test_lock_runs_upload import RunsSession

    session = RunsSession()
    monkeypatch.setattr(cli_module, "_session_from_env", lambda: session)
    return session


def test_runs_dry_run_prints_the_plan_and_writes_nothing(runs_lock_file, runs_session):
    out = CliRunner().invoke(
        cli, ["upload", str(runs_lock_file), "--mode", "real", "-e", "181", "--dry-run"]
    )
    assert out.exit_code == 0, out.output
    assert "experiment 181 (base lock OK)" in out.output
    assert "2 to append, 0 already live of 2" in out.output
    assert "Dry run - not uploading" in out.output
    assert runs_session.crawler_uploads == {} and runs_session.experiment_puts == []


def test_runs_dry_run_reports_a_refusal_and_exits_nonzero(runs_lock_file, monkeypatch):
    from .test_lock_runs_upload import RunsSession

    monkeypatch.setattr(cli_module, "_session_from_env", lambda: RunsSession(live_mode="test"))
    out = CliRunner().invoke(
        cli, ["upload", str(runs_lock_file), "--mode", "real", "-e", "181", "--dry-run"]
    )
    assert out.exit_code == 1
    assert "Refused: experiment 181 is a test experiment" in out.output


def test_runs_dry_run_needs_an_experiment_id(runs_lock_file, runs_session):
    """The -e refusal is the first check, so it is what a dry run says too."""
    out = CliRunner().invoke(cli, ["upload", str(runs_lock_file), "--mode", "real", "--dry-run"])
    assert out.exit_code == 1
    assert "Refused: a runs lock appends to an experiment: pass -e <id>" in out.output
    assert runs_session.calls == []


def test_runs_upload_prints_queue_counts_and_verifies_what_it_appended(
    runs_lock_file, runs_session
):
    out = CliRunner().invoke(cli, ["upload", str(runs_lock_file), "--mode", "real", "-e", "181"])
    assert out.exit_code == 0, out.output
    assert "Queues: 2 (skipped 0 already live)" in out.output
    assert "kind=runs" in out.output
    assert "BOUND  experiment 181" in out.output
    assert "structure: identical" in out.output
    assert sorted(runs_session.crawler_uploads) == [10, 11]


def test_runs_upload_exits_nonzero_when_the_queues_did_not_land(
    runs_lock_file, runs_session, monkeypatch
):
    """The post-upload verify is the check that the append worked: an API that
    accepted the POST and stored nothing must not read as a clean upload."""
    monkeypatch.setattr(runs_session, "existing_queues", [])
    original = runs_session.post_file
    monkeypatch.setattr(
        runs_session,
        "post_file",
        lambda *a, **k: (original(*a, **k), runs_session.existing_queues.clear())[0],
    )
    out = CliRunner().invoke(cli, ["upload", str(runs_lock_file), "--mode", "real", "-e", "181"])
    assert out.exit_code == 1, out.output
    assert "queues.length" in out.output
