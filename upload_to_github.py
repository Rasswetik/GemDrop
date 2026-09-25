"""Upload the GemDrop source to a GitHub repository with Git or GitHub CLI.

Examples:
    python upload_to_github.py https://github.com/USER/gemdrop.git
    python upload_to_github.py --create gemdrop
    python upload_to_github.py --create gemdrop --public
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_FILES = [
    'app.py', 'requirements.txt', 'README.md', '.gitignore',
    'upload_to_github.py', 'upload_to_github.bat', 'templates', 'static', 'data/.gitkeep',
]
GITHUB_URL = re.compile(
    r'(?:https://github\.com/|git@github\.com:)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?$'
)


def git(*args, check=True):
    return subprocess.run(['git', *args], cwd=ROOT, check=check, text=True)


def output(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description='Загрузить исходники GemDrop на GitHub.')
    target = parser.add_mutually_exclusive_group()
    target.add_argument('repository', nargs='?', help='Ссылка на существующий репозиторий GitHub')
    target.add_argument('--create', metavar='NAME', help='Создать репозиторий в своём GitHub-аккаунте')
    parser.add_argument('--public', action='store_true', help='Открытый репозиторий (для --create; по умолчанию закрытый)')
    args = parser.parse_args()

    if args.public and not args.create:
        parser.error('--public используется только с --create')
    if args.repository and not GITHUB_URL.fullmatch(args.repository):
        parser.error('Укажите адрес вида https://github.com/USER/REPO.git или git@github.com:USER/REPO.git')
    if args.create and not re.fullmatch(r'[A-Za-z0-9_.-]+', args.create):
        parser.error('Имя репозитория может содержать буквы, цифры, точку, дефис и подчёркивание.')
    if not shutil.which('git'):
        parser.error('Установите Git: https://git-scm.com/downloads')
    if args.create and not shutil.which('gh'):
        parser.error('Для создания репозитория установите GitHub CLI и выполните gh auth login.')

    try:
        if not (ROOT / '.git').exists():
            git('init', '-b', 'main')

        remote = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=ROOT,
                                capture_output=True, text=True)
        existing = remote.stdout.strip() if remote.returncode == 0 else ''
        if args.create and existing:
            parser.error('У проекта уже есть origin. Запустите скрипт без --create.')
        if args.repository and existing and existing.rstrip('/') != args.repository.rstrip('/'):
            parser.error('origin указывает на другой репозиторий. Скрипт не будет менять его автоматически.')
        if not args.repository and not args.create and not existing:
            parser.error('Укажите ссылку на GitHub-репозиторий или --create ИМЯ.')
        if existing and not GITHUB_URL.fullmatch(existing):
            parser.error('Текущий origin не указывает на GitHub.')

        available = [file for file in PROJECT_FILES if (ROOT / file).exists()]
        # Keep commits limited to this project's files, including removal of a
        # render.yaml that older versions of this script may have committed.
        staged = output('diff', '--cached', '--name-only').splitlines()
        if staged:
            raise RuntimeError('Сначала сохраните уже подготовленные в Git изменения отдельным коммитом.')
        previously_tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', '--', 'render.yaml'],
                                            cwd=ROOT, capture_output=True).returncode == 0
        if previously_tracked:
            git('rm', '--cached', '--', 'render.yaml')
        git('add', '-A', '--', *available)
        changed = subprocess.run(['git', 'diff', '--cached', '--quiet', '--', *available, 'render.yaml'], cwd=ROOT).returncode
        if changed == 1:
            git('commit', '-m', 'Add or update GemDrop project')
        elif changed != 0:
            raise RuntimeError('Не удалось проверить изменения перед коммитом.')
        else:
            print('Исходники уже закоммичены; отправляем текущую ветку.')

        if args.create:
            subprocess.run(['gh', 'repo', 'create', args.create,
                            '--public' if args.public else '--private',
                            '--source', str(ROOT), '--remote', 'origin'], check=True, cwd=ROOT)
        elif args.repository and not existing:
            git('remote', 'add', 'origin', args.repository)

        branch = output('branch', '--show-current')
        if not branch:
            raise RuntimeError('Не удалось определить текущую ветку Git.')
        git('push', '-u', 'origin', branch)
        print(f'Готово: ветка {branch} загружена в {output("remote", "get-url", "origin")}')
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f'Ошибка: {exc}\nПроверьте авторизацию GitHub, права на репозиторий и настройки Git.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
