"""Tests for chat_export.py — message formatting and HTML escaping."""
import sqlite3
import pytest
from chat_export import _format_content, _escape_html, _resolve_sender_any


@pytest.fixture
def contact_db(tmp_path):
    """Create a minimal contact.db with remark/nick/alias rows."""
    p = tmp_path / 'contact.db'
    con = sqlite3.connect(p)
    con.execute('CREATE TABLE contact (id INTEGER, username TEXT, remark TEXT,'
                ' nick_name TEXT, alias TEXT)')
    con.executemany(
        'INSERT INTO contact VALUES (?,?,?,?,?)',
        [
            (1, 'wxid_alpha_10e8', '张老师', '张', 'zhangs'),
            (2, 'huoshuanghuan', '', '霍双欢', 'hsh2110'),
            (3, 'wxid_noRemark_10e8', '', '无备注昵称', 'nobk'),
        ])
    con.commit()
    con.close()
    return str(p)


class TestResolveSenderAny:
    def test_exact_username_returns_remark(self, contact_db):
        # wxid_alpha_10e8 row has remark 张老师
        assert _resolve_sender_any('wxid_alpha', contact_db) == '张老师'

    def test_username_without_remark_returns_nick(self, contact_db):
        assert _resolve_sender_any('huoshuanghuan', contact_db) == '霍双欢'

    def test_unsuffixed_base_resolves_to_suffixed_remark(self, contact_db):
        # message DB stores wxid_noRemark (base), remark lives on suffixed row
        assert _resolve_sender_any('wxid_noRemark', contact_db) == '无备注昵称'

    def test_unknown_uid_returns_original(self, contact_db):
        assert _resolve_sender_any('stranger_zzz', contact_db) == 'stranger_zzz'

    def test_empty_uid_returns_original(self, contact_db):
        assert _resolve_sender_any('', contact_db) == ''
        assert _resolve_sender_any(None, contact_db) is None

    def test_missing_contact_db_returns_original(self, tmp_path):
        assert _resolve_sender_any('wxid_x', str(tmp_path / 'nope.db')) == 'wxid_x'


class TestEscapeHtml:
    def test_escapes_ampersand(self):
        assert _escape_html('a & b') == 'a &amp; b'

    def test_escapes_less_than(self):
        assert _escape_html('a < b') == 'a &lt; b'

    def test_escapes_greater_than(self):
        assert _escape_html('a > b') == 'a &gt; b'

    def test_returns_empty_for_none(self):
        assert _escape_html(None) == ''

    def test_passes_plain_text_unchanged(self):
        assert _escape_html('hello world') == 'hello world'

    def test_handles_empty_string(self):
        assert _escape_html('') == ''

    def test_escapes_multiple_special_chars(self):
        assert _escape_html('<script>alert("xss")</script>') == (
            '&lt;script&gt;alert("xss")&lt;/script&gt;'
        )


class TestFormatContent:
    def test_text_message_returns_content(self):
        assert _format_content('hello world', 1, False) == 'hello world'

    def test_image_type_with_content(self):
        assert _format_content('dummy', 3, False) == '[图片]'

    def test_file_type_with_content(self):
        assert _format_content('dummy', 6, False) == '[文件]'

    def test_voice_type_with_content(self):
        assert _format_content('dummy', 34, False) == '[语音]'

    def test_video_type_with_content(self):
        assert _format_content('dummy', 43, False) == '[视频]'

    def test_emoji_type_with_content(self):
        assert _format_content('dummy', 47, False) == '[表情]'

    def test_location_type_with_content(self):
        assert _format_content('dummy', 48, False) == '[位置]'

    def test_contact_card_type_with_content(self):
        assert _format_content('dummy', 42, False) == '[名片]'

    def test_voip_type_with_content(self):
        assert _format_content('dummy', 50, False) == '[网络电话]'

    def test_none_content_returns_empty_for_any_type(self):
        assert _format_content(None, 1, False) == ''
        assert _format_content(None, 3, False) == ''
        assert _format_content(None, 50, False) == ''

    def test_group_message_strips_sender_prefix(self):
        result = _format_content('sender_name:\nactual message', 1, True)
        assert result == 'actual message'

    def test_group_message_without_prefix_unchanged(self):
        result = _format_content('plain message', 1, True)
        assert result == 'plain message'

    def test_bytes_content_passthrough(self):
        result = _format_content(b'some bytes', 1, False)
        assert result == b'some bytes'

    @pytest.mark.parametrize('msg_type,expected', [
        (1, 'raw text'),
        (3, '[图片]'),
        (6, '[文件]'),
        (34, '[语音]'),
        (43, '[视频]'),
        (47, '[表情]'),
        (48, '[位置]'),
        (42, '[名片]'),
        (50, '[网络电话]'),
    ])
    def test_all_non_text_types(self, msg_type, expected):
        result = _format_content('raw text', msg_type, False)
        assert result == expected
