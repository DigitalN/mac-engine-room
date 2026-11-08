# PyInstaller spec for Mac Engine Room (called Pythonflow until 1.2.7; the bundle identifier
# kept the old name so installed copies keep updating)
# Builds a menu-bar-only app (LSUIElement): no Dock icon until the dashboard is opened.
# Data lives in ~/Library/Application Support/Mac Engine Room, logs in ~/Library/Logs/Mac Engine Room.

import os
import re

ICON_PATH = os.path.join(SPECPATH, 'MacEngineRoom.icns')
# The version lives in main.py only.
with open(os.path.join(SPECPATH, 'main.py'), encoding='utf-8') as f:
    VERSION = re.search(r'^VERSION = "([^"]+)"', f.read(), re.MULTILINE).group(1)

a = Analysis(
    ['main.py'],
    pathex=[SPECPATH],
    binaries=[],
    # The licenses travel with every copy, including ones that update themselves.
    datas=[
        ('index.html', '.'),
        (os.path.join(SPECPATH, '..', 'LICENSE'), '.'),
        (os.path.join(SPECPATH, '..', 'THIRD-PARTY-NOTICES'), '.'),
    ],
    hiddenimports=[
        'webview',
        'webview.platforms.cocoa',
        'objc',
        'Foundation',
        'AppKit',
        'WebKit',
        'PyObjCTools.AppHelper',
        'Quartz',
    ],
    hookspath=[],
    runtime_hooks=[],
    # Other pywebview backends and unused stdlib GUI toolkits. cryptography (and bcrypt, which
    # it imports) is only for pywebview's SSL certificates, and jinja2 (and markupsafe) only for
    # bottle's templates: both serve pywebview's web server, which never runs because the page
    # is passed in as html=. PyInstaller follows their deferred imports anyway (about 10 MB).
    # Anything bundled needs its notice in THIRD-PARTY-NOTICES.
    excludes=[
        'tkinter',
        'cryptography',
        'bcrypt',
        'jinja2',
        'markupsafe',
        'webview.platforms.android',
        'webview.platforms.cef',
        'webview.platforms.edgechromium',
        'webview.platforms.gtk',
        'webview.platforms.mshtml',
        'webview.platforms.qt',
        'webview.platforms.winforms',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Mac Engine Room',
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Mac Engine Room',
)

app = BUNDLE(
    coll,
    name='Mac Engine Room.app',
    icon=ICON_PATH if os.path.exists(ICON_PATH) else None,
    bundle_identifier='com.pythonflow.monitor',
    info_plist={
        'CFBundleName': 'Mac Engine Room',
        'CFBundleDisplayName': 'Mac Engine Room',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'LSUIElement': True,
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'NSHumanReadableCopyright': 'Copyright © 2026 DigitalN. GPL-3.0-or-later. Based on Powerflow by The Powerflow Team.',
    },
)
