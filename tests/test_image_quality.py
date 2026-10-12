"""HD selection must not silently reuse WeChat's tiny _t preview."""
import os
import struct

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from flask import Flask

from engine.services import media, v2_key_extract
from web.routes.api import api_bp

MD5 = 'a' * 32
KEY = b'0123456789abcdef'


def encrypted(path, content):
    path.write_bytes(media._DAT_V2_HEADER + struct.pack('<II', len(content), 0)
                     + b'\x00' + AES.new(KEY, AES.MODE_ECB).encrypt(pad(content, 16)))


@pytest.fixture
def images(tmp_path, monkeypatch):
    folder = tmp_path / 'media' / 'images'
    folder.mkdir(parents=True)
    monkeypatch.setattr(media, '_storage_roots', lambda _: [])
    monkeypatch.setattr(media, '_resolve_from_hardlink_db', lambda *args: None)
    monkeypatch.setattr(media, '_load_or_build_image_key_map',
                        lambda _: {MD5: {'aes': KEY, 'xor': 0}})
    monkeypatch.setattr(media, '_convert_wxgf', lambda _: None)
    monkeypatch.setattr(media, '_convert_wxgf_with_ffmpeg', lambda _: None)
    monkeypatch.setattr(v2_key_extract, 'extract_keys_from_mmkv', lambda *args: {})
    monkeypatch.setattr(v2_key_extract, 'is_wechat_running', lambda: True)
    def no_memory_scan(*args, **kwargs):
        pytest.fail('A successfully decrypted wxgf needs a decoder, not another key scan')
    monkeypatch.setattr(v2_key_extract, 'find_keys_for_files', no_memory_scan)
    return folder


def test_original_prefers_hd_over_decodable_medium(images):
    encrypted(images / f'{MD5}.dat', b'\xff\xd8\xff\xe0medium\xff\xd9')
    encrypted(images / f'{MD5}_h.dat', b'\xff\xd8\xff\xe0high-definition\xff\xd9')
    app = Flask(__name__)
    app.config['DECRYPTED_DIR'] = str(images.parent.parent)
    app.register_blueprint(api_bp, url_prefix='/api')
    with app.test_client() as client:
        preview = client.get(f'/api/hardlink-media?type=3&md5={MD5}')
        assert preview.data == b'\xff\xd8\xff\xe0medium\xff\xd9'
        original = client.get(f'/api/hardlink-media?type=3&md5={MD5}&quality=original')
        assert original.status_code == 200
        assert original.data == b'\xff\xd8\xff\xe0high-definition\xff\xd9'


@pytest.mark.parametrize('suffix', ['', '_t', '_h'])
def test_original_normalizes_thumbnail_references(images, suffix):
    hd = images / f'{MD5}_h.dat'
    hd.write_bytes(b'high-definition')
    info = {'md5': MD5 + suffix, 'file_name': MD5 + suffix + '.dat',
            'local_path': 'msg/attach/' + MD5 + suffix + '.dat',
            'media_type': 3, 'quality': 'original'}
    result = media._resolve_hardlink_path(str(images.parent.parent), info)
    assert os.path.samefile(result, hd)


def test_original_falls_back_when_hd_is_absent(images):
    medium = images / f'{MD5}.dat'
    medium.write_bytes(b'medium')
    result = media._resolve_hardlink_path(str(images.parent.parent),
                                        {'md5': MD5, 'media_type': 3, 'quality': 'original'})
    assert os.path.samefile(result, medium)


def test_original_md5_only_finds_hd_beside_db_medium_path(images, tmp_path, monkeypatch):
    root = tmp_path / 'source'
    folder = root / 'account' / 'msg' / 'attach'
    folder.mkdir(parents=True)
    (folder / f'{MD5}.dat').write_bytes(b'medium')
    hd = folder / f'{MD5}_h.dat'
    hd.write_bytes(b'high-definition')
    monkeypatch.setattr(media, '_storage_roots', lambda _: [str(root)])
    monkeypatch.setattr(media, '_resolve_from_hardlink_db',
                        lambda _, md5, __: f'msg/attach/{MD5}.dat' if md5 == MD5 else None)
    result = media._resolve_hardlink_path(str(images.parent.parent),
                                        {'md5': MD5, 'media_type': 3, 'quality': 'original'})
    assert os.path.samefile(result, hd)


@pytest.mark.parametrize('hd_decodable', [True, False])
def test_wxgf_uses_hd_then_thumbnail_without_memory_scan(images, hd_decodable):
    encrypted(images / f'{MD5}.dat', b'wxgf' + b'\x00' * 64)
    hd = b'\xff\xd8\xff\xe0high-definition\xff\xd9'
    thumb = b'\xff\xd8\xff\xe0tiny-preview\xff\xd9'
    encrypted(images / f'{MD5}_h.dat', hd if hd_decodable else b'wxgf' + b'\x00' * 64)
    encrypted(images / f'{MD5}_t.dat', thumb)
    app = Flask(__name__)
    with app.test_request_context('/'):
        response = media.serve_hardlink_media(str(images.parent.parent),
                                            {'md5': MD5, 'media_type': 3})
        response.direct_passthrough = False
        assert response.get_data() == (hd if hd_decodable else thumb)
        assert response.headers['X-WeChat-Image-Source'] == ('hd-image' if hd_decodable else 'thumbnail')
        assert response.cache_control.max_age == 0
        response.close()
