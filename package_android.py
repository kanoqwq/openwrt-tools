"""Attach one verified Android control app at a firmware package's root."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import runpy
import shutil

HERE=Path(__file__).resolve().parent
APK='OpenWrt-Tools.apk'
METADATA='docs/GUI-BUILD-android.json'


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def artifact(directory=None):
    directory=Path(directory or os.environ.get('OPENWRT_GUI_ANDROID_ARTIFACT',HERE/'artifacts/android'))
    metadata=directory/'OpenWrt-Tools-build.json';apk=directory/APK
    if not metadata.is_file() or not apk.is_file():
        raise ValueError('Android release APK is missing; build installer/build-android.py / 缺少 Android 正式安装器 APK')
    build=json.loads(metadata.read_text(encoding='utf-8'))
    if (build.get('protocol')!=1 or build.get('platform')!='android-aarch64' or build.get('debug') is not False or
            set(build.get('files',{}))!={APK} or not build.get('signing')):
        raise ValueError('Expected a signed ARM64 release APK / 必须使用已签名 ARM64 正式 APK')
    if build.get('source')!=runpy.run_path(str(HERE/'build-android.py'))['sources']():
        raise ValueError('Android release APK is stale; rebuild it / Android 安装器已过期，请重新编译')
    if sha(apk)!=build['files'][APK]:raise ValueError('Android APK checksum mismatch')
    if runpy.run_path(str(HERE/'mobile/apk.py'))['validate'](apk,'aarch64')!=build.get('validation'):
        raise ValueError('Android APK ABI/ELF validation mismatch')
    return apk,metadata


def attach(package,directory=None):
    apk,metadata=artifact(directory)
    package=Path(package);(package/'docs').mkdir(exist_ok=True)
    shutil.copy2(apk,package/APK)
    shutil.copy2(metadata,package/METADATA)
    shutil.copy2(HERE/'mobile/README.md',package/'docs/ANDROID-INSTALLER.md')
    return json.loads(metadata.read_text(encoding='utf-8'))


def verify_archive(archive):
    names=archive.namelist()
    if names.count(APK)!=1 or sum(PurePosixPath(n).name==APK for n in names)!=1:
        raise ValueError('Release must contain one OpenWrt-Tools.apk at its root / 发布包根目录必须包含唯一的 Android 安装器')
    build=json.loads(archive.read(METADATA))
    if (build.get('protocol')!=1 or build.get('platform')!='android-aarch64' or build.get('debug') is not False or
            set(build.get('files',{}))!={APK} or not build.get('signing')):
        raise ValueError('Invalid packaged Android release metadata')
    with archive.open(APK) as stream:
        h=hashlib.sha256()
        for block in iter(lambda:stream.read(1<<20),b''):h.update(block)
    if h.hexdigest()!=build['files'][APK]:raise ValueError('Packaged Android APK checksum mismatch')
