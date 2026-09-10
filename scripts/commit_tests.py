#!/usr/bin/env python3
"""Install a chained Git hook that tests the exact index in offline Docker."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


VERSION = 1
MARKER = '# managed-full-suite-tests-v1'


def git_binary():
    apple = Path('/Library/Developer/CommandLineTools/usr/bin/git')
    return str(apple) if apple.exists() else 'git'


def git(repo, *args):
    return subprocess.check_output([git_binary(), '-C', str(repo), *args], timeout=60)


def export_index(repo, destination):
    tree = git(repo, 'write-tree').decode().strip()
    entries = []
    for record in git(repo, 'ls-tree', '-rz', tree).split(b'\0'):
        if not record:
            continue
        metadata, name = record.split(b'\t', 1)
        mode, kind, object_id = metadata.decode().split()
        path = Path(os.fsdecode(name))
        if kind != 'blob' or mode not in ('100644', '100755') or path.is_absolute() or '..' in path.parts:
            raise RuntimeError('Unsupported staged entry; refusing an unsafe snapshot')
        entries.append((mode, object_id, path))
    requests = ''.join(object_id + '\n' for _, object_id, _ in entries).encode()
    content = subprocess.check_output([git_binary(), '-C', str(repo), 'cat-file', '--batch'],
                                      input=requests, timeout=60)
    stream = io.BytesIO(content)
    for mode, object_id, path in entries:
        actual_id, kind, length = stream.readline().decode().split()
        if actual_id != object_id or kind != 'blob':
            raise RuntimeError('Unexpected staged blob response')
        value = stream.read(int(length))
        if len(value) != int(length) or stream.read(1) != b'\n':
            raise RuntimeError('Incomplete staged blob')
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
        target.chmod(0o755 if mode == '100755' else 0o644)
    return tree


def requirement_files(snapshot):
    result = {}

    def visit(name):
        path = (snapshot / name).resolve()
        if snapshot.resolve() not in path.parents or not path.is_file():
            raise RuntimeError('Requirements must be regular files inside the staged tree')
        relative = path.relative_to(snapshot.resolve()).as_posix()
        if relative in result:
            return
        content = path.read_bytes()
        result[relative] = content
        for line in content.decode().splitlines():
            words = shlex.split(line, comments=True)
            if words and words[0] in ('-r', '--requirement'):
                visit(str(Path(relative).parent / words[1]))

    visit('requirements.txt')
    return result


def image_id(docker, image):
    return subprocess.check_output(
        [docker, 'image', 'inspect', image, '--format', '{{.Id}}'], timeout=30,
    ).decode().strip()


def ensure_image(config, snapshot, temporary):
    docker = config['docker']
    base_id = image_id(docker, config['base_image'])
    node_id = image_id(docker, config['node_image'])
    files = requirement_files(snapshot)
    identity = [str(VERSION), config['project'], base_id, node_id]
    identity.extend(name + ':' + hashlib.sha256(value).hexdigest() for name, value in sorted(files.items()))
    key = hashlib.sha256('\n'.join(identity).encode()).hexdigest()[:24]
    image = 'halfshade-commit-tests:' + key
    found = subprocess.run([docker, 'image', 'inspect', image], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
    if found.returncode == 0:
        return image
    context = temporary / 'build'
    context.mkdir()
    for name, value in files.items():
        target = context / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    lines = [
        'FROM ' + config['node_image'] + ' AS node_runtime',
        'FROM ' + config['base_image'],
        'COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node',
        'WORKDIR /commit-test-dependencies',
        'COPY . .',
    ]
    if config['project'] == 'cea':
        lines.append('RUN python -m pip uninstall -y pytradekit')
    lines.append('RUN python -m pip install -r requirements.txt pytest-asyncio==0.23.8'
                 + (' fastapi==0.115.6' if config['project'] == 'cea' else '')
                 + ' && python -m pip check')
    (context / 'Dockerfile').write_text('\n'.join(lines) + '\n')
    print('[commit tests] Building a runtime for the staged dependency versions', flush=True)
    subprocess.run([docker, 'build', '--no-cache', '-t', image, str(context)], check=True, timeout=900)
    return image


def run_container(config, snapshot, temporary, image):
    docker = config['docker']
    cidfile = temporary / 'container.id'
    command = [docker, 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--cidfile', str(cidfile),
               '--mount', 'type=bind,src=' + str(snapshot) + ',dst=/snapshot,readonly',
               '--mount', 'type=bind,src=' + str(Path(__file__).resolve()) + ',dst=/commit_tests.py,readonly',
               '-e', 'PYTHONPATH=/snapshot', '-e', 'COVERAGE_FILE=/tmp/commit-tests.coverage',
               '-w', '/snapshot', image, 'python', '/commit_tests.py', 'inside',
               '--project', config['project']]
    try:
        subprocess.run(command, check=True, timeout=900)
    finally:
        if cidfile.exists():
            container = cidfile.read_text().strip()
            if len(container) == 64 and all(c in '0123456789abcdef' for c in container):
                subprocess.run([docker, 'rm', '-f', container], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)


def inside(project):
    import importlib.metadata as metadata
    from packaging.requirements import Requirement
    # Tests can write logs and reports inside the disposable container only.
    snapshot = Path(tempfile.mkdtemp(prefix='suite-', dir='/tmp'))
    shutil.copytree('/snapshot', snapshot, dirs_exist_ok=True)
    os.chdir(snapshot)
    os.environ['PYTHONPATH'] = str(snapshot)
    for content in requirement_files(snapshot).values():
        for line in content.decode().splitlines():
            text = line.strip()
            if not text or text.startswith(('#', '-r', '--requirement')):
                continue
            dependency = Requirement(text)
            if dependency.marker and not dependency.marker.evaluate():
                continue
            installed = metadata.distribution(dependency.name)
            if dependency.url:
                revision = dependency.url.rsplit('@', 1)[-1]
                actual = json.loads(installed.read_text('direct_url.json') or '{}')
                if actual.get('vcs_info', {}).get('commit_id') != revision:
                    raise RuntimeError('Installed Git dependency does not match the staged revision')
            elif installed.version not in dependency.specifier:
                raise RuntimeError('Installed dependency does not match staged requirements: ' + dependency.name)
    subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)
    command = [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider',
               '-o', 'required_plugins=pytest-asyncio>=0.23.8',
               '-W', 'error::pytest.PytestUnhandledCoroutineWarning']
    subprocess.run(command, check=True)
    if project == 'cea':
        tests = sorted(str(path) for path in (snapshot / 'tests').glob('test_webui_frontend_*.js'))
        if not tests:
            raise RuntimeError('CEA frontend suite is missing from the staged tree')
        subprocess.run(['node', '--test', *tests], check=True)


def run(config_path, prepare_only=False):
    config = json.loads(Path(config_path).read_text())
    repo = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel').decode().strip())
    if config.get('previous_hook'):
        subprocess.run([config['previous_hook']], cwd=repo, check=True)
    with tempfile.TemporaryDirectory(prefix='commit-tests-') as directory:
        temporary = Path(directory)
        snapshot = temporary / 'snapshot'
        snapshot.mkdir()
        tree = export_index(repo, snapshot)
        print('[commit tests] Full suite for staged tree ' + tree[:12], flush=True)
        image = ensure_image(config, snapshot, temporary)
        if not prepare_only:
            run_container(config, snapshot, temporary, image)
        if git(repo, 'write-tree').decode().strip() != tree:
            raise RuntimeError('The index changed while tests ran; retry the commit')
    if not prepare_only:
        print('[commit tests] Full suite passed; continuing existing privacy checks', flush=True)


def install(args):
    repo = Path(args.repo).resolve()
    common = Path(git(repo, 'rev-parse', '--git-common-dir').decode().strip())
    common = common if common.is_absolute() else repo / common
    hooks = common.resolve() / 'hooks'
    hooks.mkdir(exist_ok=True)
    destination = hooks / 'pre-commit'
    config_path = hooks / 'full-suite-tests.json'
    runner = hooks / 'full_suite_tests.py'
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if destination.exists() and MARKER not in destination.read_text():
        backup = hooks / ('pre-commit.before-full-tests-' + hashlib.sha256(destination.read_bytes()).hexdigest()[:12])
        if not backup.exists():
            shutil.copy2(destination, backup)
        config['previous_hook'] = str(backup)
    config.update(project=args.project, docker=str(Path(args.docker).resolve()),
                  base_image=args.base_image, node_image=args.node_image)
    shutil.copy2(__file__, runner)
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    script = '#!/bin/sh\n' + MARKER + '\nexec ' + ' '.join(shlex.quote(value) for value in
        [str(Path(sys.executable).resolve()), str(runner), 'run', '--config', str(config_path)]) + '\n'
    destination.write_text(script)
    destination.chmod(0o755)
    print('Installed repository hook: ' + str(destination))
    print('Existing core.hooksPath is unchanged; privacy guard must chain repository hooks.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'run', 'prepare', 'inside'])
    parser.add_argument('--repo', default='.')
    parser.add_argument('--project', choices=['pytradekit', 'cea'])
    parser.add_argument('--docker', default=shutil.which('docker') or '/usr/local/bin/docker')
    parser.add_argument('--base-image', default='cea-okx-keepalive-test:20260909')
    parser.add_argument('--node-image', default='node:20-bookworm-slim')
    parser.add_argument('--config')
    args = parser.parse_args()
    try:
        if args.action == 'install':
            if not args.project:
                parser.error('--project is required for installation')
            install(args)
        elif args.action == 'inside':
            inside(args.project)
        else:
            if not args.config:
                parser.error('--config is required')
            run(args.config, prepare_only=args.action == 'prepare')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('[commit tests] BLOCKED: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
