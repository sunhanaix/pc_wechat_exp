"""Tests for wxgf(H.265) image handling helpers (engine/services/media.py).

全部使用临时文件与合成 zip，不涉及真实微信数据，也不依赖本机是否装了 ffmpeg。
"""
import os
import zipfile

import pytest

from engine.services import media


class TestSniffImageMime:
    def test_jpeg(self):
        assert media._sniff_image_mime(b"\xff\xd8\xff\xe0abcd") == "image/jpeg"

    def test_png(self):
        assert media._sniff_image_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"

    def test_gif(self):
        assert media._sniff_image_mime(b"GIF89a....") == "image/gif"

    def test_webp(self):
        assert media._sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"

    def test_bmp(self):
        assert media._sniff_image_mime(b"BM\x00\x00") == "image/bmp"

    def test_wxgf_is_not_servable(self):
        assert media._sniff_image_mime(b"wxgf\x13\x00\x02\x04") == "application/octet-stream"

    def test_empty(self):
        assert media._sniff_image_mime(b"") == "application/octet-stream"


class TestSiblingThumbnail:
    def test_prefers_hd_suffix(self, tmp_path):
        original = tmp_path / "abc.dat"
        original.write_bytes(b"wxgf-original")
        (tmp_path / "abc_t.dat").write_bytes(b"jpeg-thumb")
        (tmp_path / "abc_h.dat").write_bytes(b"jpeg-big-thumb")
        got = media._find_sibling_thumbnail(str(original))
        assert got is not None and got.endswith("abc_h.dat")

    def test_falls_back_to_h_suffix(self, tmp_path):
        original = tmp_path / "abc.dat"
        original.write_bytes(b"x")
        (tmp_path / "abc_h.dat").write_bytes(b"jpeg-big-thumb")
        assert media._find_sibling_thumbnail(str(original)).endswith("abc_h.dat")

    def test_none_when_no_sibling(self, tmp_path):
        original = tmp_path / "abc.dat"
        original.write_bytes(b"x")
        assert media._find_sibling_thumbnail(str(original)) is None

    def test_thumbnail_input_finds_full_size_sibling(self, tmp_path):
        thumb = tmp_path / "abc_t.dat"
        thumb.write_bytes(b"x")
        (tmp_path / "abc_t_t.dat").write_bytes(b"y")
        assert media._find_sibling_thumbnail(str(thumb)) is None
        (tmp_path / "abc_h.dat").write_bytes(b"hd")
        assert media._find_sibling_thumbnail(str(thumb)).endswith("abc_h.dat")


class TestInstallFromZip:
    def _make_zip(self, path, member_name, size=200000):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(member_name, b"F" * size)
        return path

    def test_extracts_bin_ffmpeg(self, tmp_path):
        zp = self._make_zip(str(tmp_path / "ff.zip"), "ffmpeg-7.0-essentials_build/bin/ffmpeg.exe")
        dest = tmp_path / "tools"
        out = media.install_ffmpeg_from_zip(zp, str(dest))
        assert os.path.isfile(out)
        assert os.path.basename(out) == "ffmpeg.exe"
        assert os.path.getsize(out) == 200000

    def test_raises_when_missing(self, tmp_path):
        zp = self._make_zip(str(tmp_path / "ff.zip"), "readme.txt", size=10)
        with pytest.raises(ValueError):
            media.install_ffmpeg_from_zip(zp, str(tmp_path / "tools"))


class TestStatusAndPaths:
    def test_status_shape(self):
        st = media.wxgf_status()
        for key in ("supported", "installed", "toolsDir", "targetPath", "downloadUrls"):
            assert key in st
        assert isinstance(st["downloadUrls"], list) and st["downloadUrls"]
        assert all("url" in u and "name" in u for u in st["downloadUrls"])

    def test_tools_dir_is_ffmpeg_target(self):
        st = media.wxgf_status()
        assert os.path.normcase(st["targetPath"]) == os.path.normcase(
            os.path.join(media.tools_dir(), "ffmpeg.exe"))

    def test_search_paths_include_tools_dir(self):
        paths = [os.path.normcase(p) for p in media._ffmpeg_search_paths()]
        assert os.path.normcase(os.path.join(media.tools_dir(), "ffmpeg.exe")) in paths
