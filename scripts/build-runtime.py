#!/usr/bin/env python3
"""Build the offline Python dependency closure shipped in a Modal package."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON = '3.13.11'
UV = '0.13.10'
TARGETS = {
    'macos-aarch64': ('macos', 'aarch64', 'none', 'aarch64-apple-darwin'),
    'linux-aarch64': ('linux', 'aarch64', 'gnu', 'aarch64-unknown-linux-gnu'),
    'linux-x86_64': ('linux', 'x86_64', 'gnu', 'x86_64-unknown-linux-gnu'),
}


def build(platform, output):
    uv = os.environ.get('UV', 'uv')
    if subprocess.check_output([uv, '--version'], text=True).split()[1] != UV:
        raise ValueError(f'Build runtime packages with uv=={UV}')
    system, arch, libc, target = TARGETS[platform]
    with tempfile.TemporaryDirectory(prefix='magicmtn-modal-runtime-') as temporary:
        root = Path(temporary)
        managed = root / 'managed'
        key = f'cpython-{PYTHON}-{system}-{arch}-{libc}'
        subprocess.run([uv, '--no-config', 'python', 'install', '--no-bin', '--install-dir', str(managed), key], check=True)
        python = managed / key
        if not python.is_dir():
            raise ValueError('Managed Python installation missing')
        site = python / 'lib/python3.13/site-packages'
        subprocess.run([uv, '--no-config', 'pip', 'install', '--target', str(site),
                        '--python-version', PYTHON, '--python-platform', target,
                        '--only-binary', ':all:', '--require-hashes', '-r', str(ROOT / 'requirements.lock')], check=True)
        output.mkdir(parents=True, exist_ok=True)
        archive = output / 'runtime.tar.gz'
        # Resolve symlinks while packaging: installers accept regular files only.
        with archive.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(python.rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    info = tar.gettarinfo(str(path.resolve()), arcname='python/' + str(path.relative_to(python)))
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ''
                    info.type = tarfile.REGTYPE
                    info.linkname = ''
                    with path.open('rb') as data:
                        tar.addfile(info, data)
        spec = {'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'environment': {'MAGICMTN_MODAL_PYTHON': 'python/bin/python3.13',
                                'SSL_CERT_FILE': 'python/lib/python3.13/site-packages/certifi/cacert.pem'},
                'check': {'executable': 'python/bin/python3.13',
                          'args': ['-I', '-c', 'import modal, ssl; assert modal.__version__ == "1.6.1"; assert ssl.create_default_context().get_ca_certs()']}}
        (output / 'runtime.json').write_text(json.dumps(spec, indent=2) + '\n')
        return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=TARGETS, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(build(args.platform, args.output.resolve()))


if __name__ == '__main__':
    main()
