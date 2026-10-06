from pathlib import Path

root = Path(SPECPATH).parents[1]
assets = root / 'build/macos-assets'
a = Analysis(
    [str(root / 'tools/macos_entry.py')],
    pathex=[str(root)],
    binaries=[(str(assets / 'ffmpeg'), 'bin')],
    datas=[(str(root / 'refmod_builder/icon.svg'), 'refmod_builder'),
           (str(assets / 'licenses'), 'licenses')],
    hiddenimports=['PySide6.QtSvg', 'refmod_builder.smoke'],
    excludes=['pytest', 'numpy', 'safetensors', 'imageio_ffmpeg', 'tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='refmodBuilder',
          debug=False, strip=False, upx=False, console=False, target_arch='arm64',
          codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='refmodBuilder')
app = BUNDLE(coll, name='refmodBuilder.app', icon=str(assets / 'refmodBuilder.icns'),
             bundle_identifier='com.cyberworm1.refmodbuilder', version='0.1.1',
             info_plist={'CFBundleDisplayName': 'refmodBuilder', 'CFBundleShortVersionString': '0.1.1',
                         'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '26.0',
                         'NSLocalNetworkUsageDescription': 'Connect to your configured ComfyUI backend to encode reference packages.'})
