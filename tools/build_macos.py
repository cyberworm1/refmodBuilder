"""Build a native Apple Silicon .app and ZIP on macOS."""
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def main():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('Run this build with native arm64 Python on an Apple Silicon Mac.')
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from imageio_ffmpeg import get_ffmpeg_exe

    os.chdir(ROOT)
    stage = ROOT / 'build/macos-assets'
    stage.mkdir(parents=True, exist_ok=True)
    iconset = stage / 'refmodBuilder.iconset'
    iconset.mkdir(exist_ok=True)
    app = QGuiApplication([])
    svg = QSvgRenderer(str(ROOT / 'refmod_builder/icon.svg'))
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            image = QImage(size * scale, size * scale, QImage.Format_ARGB32)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            svg.render(painter)
            painter.end()
            suffix = '@2x' if scale == 2 else ''
            image.save(str(iconset / f'icon_{size}x{size}{suffix}.png'))
    command('iconutil', '-c', 'icns', iconset, '-o', stage / 'refmodBuilder.icns')
    shutil.copy2(get_ffmpeg_exe(), stage / 'ffmpeg')
    (stage / 'ffmpeg').chmod(0o755)
    command('lipo', stage / 'ffmpeg', '-verify_arch', 'arm64')

    # Preserve upstream license notices for the packaged runtime dependencies.
    notices = stage / 'licenses'
    notices.mkdir(exist_ok=True)
    for package in ('PySide6', 'PySide6-Addons', 'PySide6-Essentials', 'shiboken6', 'Pillow',
                    'httpx', 'httpcore', 'anyio', 'certifi', 'h11', 'idna', 'typing_extensions', 'imageio-ffmpeg'):
        dist = importlib.metadata.distribution(package)
        for item in dist.files or []:
            if any(word in str(item).lower() for word in ('license', 'copying', 'notice')):
                source = Path(dist.locate_file(item))
                if source.is_file():
                    target = notices / package / str(item).replace('../', '')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    for prefix in (Path(sys.base_prefix), *Path(sys.base_prefix).parents):
        license_file = prefix / 'LICENSE'
        if license_file.is_file():
            shutil.copy2(license_file, notices / 'Python-LICENSE.txt')
            break
    ffmpeg_license = subprocess.run([str(stage / 'ffmpeg'), '-L'], check=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (notices / 'FFmpeg-LICENSE-and-build.txt').write_text(ffmpeg_license.stdout)
    shutil.copy2(ROOT / 'packaging/macos/THIRD_PARTY.txt', notices / 'THIRD_PARTY.txt')
    command(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', ROOT / 'packaging/macos/refmodBuilder.spec')
    bundle = ROOT / 'dist/refmodBuilder.app'
    command('codesign', '--verify', '--deep', '--strict', bundle)
    command('lipo', bundle / 'Contents/MacOS/refmodBuilder', '-verify_arch', 'arm64')
    # A stripped PATH verifies the bundle does not depend on Homebrew FFmpeg.
    env = dict(os.environ, PATH='/usr/bin:/bin:/usr/sbin:/sbin', QT_QPA_PLATFORM='offscreen')
    command(bundle / 'Contents/MacOS/refmodBuilder', '--smoke-test', env=env)
    archive = ROOT / 'dist/refmodBuilder-0.1.1-macos-arm64.zip'
    if archive.exists():
        archive.unlink()
    command('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', bundle, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (archive.parent / (archive.name + '.sha256')).write_text(f'{digest}  {archive.name}\n')
    versions = {d.metadata['Name']: d.version for d in importlib.metadata.distributions()}
    report = {'python': sys.version, 'platform': platform.platform(), 'architecture': platform.machine(),
              'packages': versions, 'artifact': archive.name, 'sha256': digest,
              'signing': 'ad-hoc (not Developer ID signed or notarized)'}
    (archive.parent / 'macos-build-info.json').write_text(json.dumps(report, indent=2))
    print(f'Built {archive} ({archive.stat().st_size / 1024**2:.1f} MiB)')


if __name__ == '__main__':
    main()
