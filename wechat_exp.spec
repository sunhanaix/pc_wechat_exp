# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('src', 'src'), ('tools/silk_decoder.exe', 'tools'), ('tools/silk_decoder.c', 'tools')]
binaries = []
hiddenimports = ['Crypto.Cipher.AES', 'Crypto.Util.Padding', 'flask', 'werkzeug', 'jinja2', 'blackboxprotobuf', 'zstandard', 'openpyxl', 'jieba', 'jieba.posseg', 'requests', 'pypinyin']
tmp_ret = collect_all('jieba')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['src/main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', '_pytest', 'tkinter', '_tkinter', 'turtle', 'idlelib', 'ensurepip', 'pip', 'setuptools', 'wheel', 'pkg_resources', 'multiprocessing', 'concurrent.futures.process', 'lib2to3', 'xmlrpc', 'pydoc', 'doctest', 'bdb'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='wechat_exp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='file_version_info.txt',
)
