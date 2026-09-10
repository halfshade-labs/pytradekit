"""Regression checks for staged-only, fail-closed full-suite hooks."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


spec = importlib.util.spec_from_file_location(
    'commit_tests', Path(__file__).resolve().parents[1] / 'scripts' / 'commit_tests.py',
)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repo = tmp_path / 'repository'
    repo.mkdir()
    # Tests may run from an outer commit carrying its own index environment.
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        monkeypatch.delenv(name, raising=False)
    hook.git(repo, 'init', '-q')
    (repo / 'requirements.txt').write_text('pytest==8.0.2\n')
    (repo / 'code.py').write_text('value = "staged"\n')
    hook.git(repo, 'add', '.')
    monkeypatch.chdir(repo)
    return repo


def test_snapshot_uses_index_and_excludes_unstaged_and_untracked(repository, tmp_path):
    (repository / 'code.py').write_text('value = "unstaged"\n')
    (repository / 'local-secret.txt').write_text('synthetic-private-data')
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()

    hook.export_index(repository, snapshot)

    assert (snapshot / 'code.py').read_text() == 'value = "staged"\n'
    assert not (snapshot / 'local-secret.txt').exists()
    assert (repository / 'code.py').read_text() == 'value = "unstaged"\n'


def test_snapshot_honors_alternate_index(repository, tmp_path, monkeypatch):
    original = hook.git(repository, 'write-tree')
    monkeypatch.setenv('GIT_INDEX_FILE', str(tmp_path / 'alternate-index'))
    hook.git(repository, 'read-tree', original.decode().strip())
    (repository / 'code.py').write_text('value = "partial-commit"\n')
    hook.git(repository, 'add', 'code.py')
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()

    hook.export_index(repository, snapshot)

    assert 'partial-commit' in (snapshot / 'code.py').read_text()
    monkeypatch.delenv('GIT_INDEX_FILE')
    assert hook.git(repository, 'write-tree') == original


def test_export_ignore_cannot_hide_staged_code_from_tests(repository, tmp_path):
    (repository / '.gitattributes').write_text('code.py export-ignore\n')
    hook.git(repository, 'add', '.gitattributes')
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()

    hook.export_index(repository, snapshot)

    assert (snapshot / 'code.py').read_text() == 'value = "staged"\n'


def configure_run(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'project': 'pytradekit'}))
    monkeypatch.setattr(hook, 'ensure_image', lambda *args: 'test-image')
    return config


def test_failed_full_suite_aborts_hook(repository, tmp_path, monkeypatch):
    config = configure_run(tmp_path, monkeypatch)

    def fail(*args):
        raise subprocess.CalledProcessError(1, ['docker', 'run'])

    monkeypatch.setattr(hook, 'run_container', fail)
    with pytest.raises(subprocess.CalledProcessError):
        hook.run(config)


def test_changed_index_after_tests_blocks_commit(repository, tmp_path, monkeypatch):
    config = configure_run(tmp_path, monkeypatch)

    def mutate(*args):
        (repository / 'code.py').write_text('value = "changed-after-validation"\n')
        hook.git(repository, 'add', 'code.py')

    monkeypatch.setattr(hook, 'run_container', mutate)
    with pytest.raises(RuntimeError, match='index changed'):
        hook.run(config)


def test_repeated_invocations_always_run_entire_suite(repository, tmp_path, monkeypatch):
    config = configure_run(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(hook, 'run_container', lambda *args: calls.append(args))

    hook.run(config)
    hook.run(config)

    assert len(calls) == 2


def test_requirements_cannot_escape_staged_snapshot(repository, tmp_path):
    (repository / 'requirements.txt').write_text('-r ../outside.txt\n')
    (repository.parent / 'outside.txt').write_text('pytest==8.0.2\n')
    with pytest.raises(RuntimeError, match='inside the staged tree'):
        hook.requirement_files(repository)
