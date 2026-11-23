"""Copy the public subset of this repository into a separate working tree (the public GitHub repo).

The development repository stays private and unchanged. This script
  1. takes the files Git knows about (committed at --ref, or the working tree with --worktree),
  2. keeps only the paths allowed by publish/public_manifest.txt,
  3. mirrors them into --dest (files no longer allowed are deleted there; .git is never touched),
  4. lays publish/overlay/ on top (README.md, LICENSE, .gitignore for the public repo),
  5. scans the result for private markers and oversized files, and stops on any finding.
It never commits or pushes: review `git status` in --dest, then commit and push by hand.

    python scripts/publish_public.py --dest ../../public/beetle-life-lab   # from HEAD
    python scripts/publish_public.py --dest ... --worktree                                       # uncommitted state
    python scripts/publish_public.py --dest ... --dry-run                                        # list only

Private markers are read from publish/private_markers.txt (one regular expression per line;
the file is not published). Built-in checks: absolute user / drive paths and real device IDs.
"""
import argparse
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
REPO = HERE.parent                                   # beetle-life-lab
PUBLISH = REPO / 'publish'
MAX_BYTES = 5 * 1024 * 1024                          # larger files need a deliberate decision
BUILTIN_MARKERS = [
    r'[A-Za-z]:[\\/]+Users[\\/]',                    # Windows user profile paths
    r'\b[D-Z]:[\\/]+arduino_contest',                # the development drive layout
    r'\b(?:xiao|nano)-(?!0123456789ab\b|ffffffffffff\b)[0-9a-f]{12}\b',  # real device IDs (MAC based)
]
TEXT_SUFFIXES = {'.py', '.ps1', '.js', '.cjs', '.html', '.css', '.json', '.md', '.txt', '.ini', '.h', '.hpp',
                 '.c', '.cpp', '.yml', '.yaml', '.toml', '.cfg', '.properties', '.gitignore', '.node-version', ''}


def pattern_regex(pattern):
    """Glob with ** (any depth) and * (one segment); a trailing / means everything below."""
    if pattern.endswith('/'):
        pattern += '**'
    out, i = '', 0
    while i < len(pattern):
        if pattern.startswith('**/', i):
            out += '(?:.*/)?'; i += 3
        elif pattern.startswith('**', i):
            out += '.*'; i += 2
        elif pattern[i] == '*':
            out += '[^/]*'; i += 1
        elif pattern[i] == '?':
            out += '[^/]'; i += 1
        else:
            out += re.escape(pattern[i]); i += 1
    return re.compile(out + r'\Z')


def load_manifest(path):
    allow, deny = [], []
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        (deny if line.startswith('!') else allow).append(pattern_regex(line.lstrip('!')))
    return allow, deny


def allowed(rel, allow, deny):
    return any(p.match(rel) for p in allow) and not any(p.match(rel) for p in deny)


def git(*args, binary=False):
    out = subprocess.run(['git', *args], cwd=REPO, check=True, capture_output=True)
    return out.stdout if binary else out.stdout.decode('utf-8')


def source_files(ref, worktree):
    """{relative posix path: bytes} for every file Git knows about under beetle-life-lab."""
    if worktree:
        names = git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
        return {n: (REPO / n).read_bytes() for n in names if n and (REPO / n).is_file()}
    prefix = git('rev-parse', '--show-prefix').strip()          # e.g. 'beetle-life-lab/'
    data = git('archive', '--format=tar', ref, '--', '.', binary=True)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                name = member.name[len(prefix):] if member.name.startswith(prefix) else member.name
                files[name] = tar.extractfile(member).read()
    return files


def overlay_files():
    root = PUBLISH / 'overlay'
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def scan(files, markers):
    problems = []
    for rel, data in sorted(files.items()):
        if len(data) > MAX_BYTES:
            problems.append(f'{rel}: {len(data):,} bytes (> {MAX_BYTES:,})')
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix not in TEXT_SUFFIXES and b'\0' in data[:4096]:
            continue                                            # binary (images, etc.)
        text = data.decode('utf-8', errors='replace')
        for marker in markers:
            for m in marker.finditer(text):
                line = text.count('\n', 0, m.start()) + 1
                problems.append(f'{rel}:{line}: private marker /{marker.pattern}/ -> {m.group(0)!r}')
    return problems


def mirror(dest, files):
    """Make dest contain exactly `files` (outside .git). Returns (written, deleted)."""
    dest.mkdir(parents=True, exist_ok=True)
    written = deleted = 0
    for path in sorted(dest.rglob('*'), reverse=True):
        rel = path.relative_to(dest).as_posix()
        if rel == '.git' or rel.startswith('.git/') or rel.split('/')[0] in KEEP_IN_DEST:
            continue
        if path.is_file() and rel not in files:
            path.unlink(); deleted += 1
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    for rel, data in files.items():
        target = dest / rel
        if target.is_file() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data); written += 1
    return written, deleted


# Local, untracked folders a developer may create in the public clone (never published: see overlay .gitignore).
KEEP_IN_DEST = {'.venv', 'captures', '.platformio-local'}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dest', type=Path, required=True, help='working tree of the public repository')
    ap.add_argument('--ref', default='HEAD', help='commit to publish (default HEAD)')
    ap.add_argument('--worktree', action='store_true', help='publish the working tree instead of a commit')
    ap.add_argument('--dry-run', action='store_true', help='list the files and run the checks only')
    args = ap.parse_args()

    dest = args.dest.resolve()
    if dest == REPO or REPO in dest.parents or dest in REPO.parents:
        sys.exit('--dest must be outside the development repository')

    allow, deny = load_manifest(PUBLISH / 'public_manifest.txt')
    source = source_files(args.ref, args.worktree)
    files = {rel: data for rel, data in source.items() if allowed(rel, allow, deny)}
    files.update(overlay_files())

    markers = [re.compile(p) for p in BUILTIN_MARKERS]
    marker_file = PUBLISH / 'private_markers.txt'
    if marker_file.is_file():
        for line in marker_file.read_text(encoding='utf-8').splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                markers.append(re.compile(line.strip(), re.IGNORECASE))
    else:
        print('warning: publish/private_markers.txt not found; only the built-in checks run')

    origin = 'working tree' if args.worktree else f'{args.ref} ({git("rev-parse", "--short", args.ref).strip()})'
    top = {}
    for rel in files:
        key = '/'.join(rel.split('/')[:2]) if '/' in rel else rel
        top[key] = top.get(key, 0) + 1
    print(f'source: {origin}; {len(files)} of {len(source)} files selected ({sum(map(len, files.values())):,} bytes)')
    for key in sorted(top):
        print(f'  {top[key]:4d}  {key}')

    problems = scan(files, markers)
    if problems:
        print(f'\nSTOPPED: {len(problems)} finding(s). Fix them (or the manifest) and run again:')
        for p in problems:
            print('  ' + p)
        sys.exit(1)
    print('checks: no private markers, no file over the size limit')
    if args.dry_run:
        return
    written, deleted = mirror(dest, files)
    print(f'mirrored into {dest}: {written} written, {deleted} deleted')
    if (dest / '.git').is_dir():
        print(subprocess.run(['git', 'status', '--short'], cwd=dest, capture_output=True, text=True).stdout or 'no changes')
    print('Next: review the changes in the public tree, run its tests, then commit and push by hand.')


if __name__ == '__main__':
    main()
