"""Tests for engine.shard_select — time-aware message shard selection."""
import os
import sqlite3
from datetime import date, datetime

from engine import shard_select as ss


def _make_msg_db(dirpath, name, start_dt, end_dt):
    os.makedirs(dirpath, exist_ok=True)
    path = os.path.join(dirpath, name)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    conn.execute("CREATE TABLE Msg_deadbeef (create_time INTEGER)")
    conn.execute("INSERT INTO Msg_deadbeef (create_time) VALUES (?), (?)",
                 (start_dt.timestamp(), end_dt.timestamp()))
    conn.commit()
    conn.close()
    return path


def _touch(msg_dir, name, mtime):
    os.makedirs(msg_dir, exist_ok=True)
    p = os.path.join(msg_dir, name)
    open(p, 'a').close()
    os.utime(p, (mtime, mtime))
    return p


def test_scan_decrypted_ranges(tmp_path):
    msg = tmp_path / 'dec' / 'message'
    _make_msg_db(str(msg), 'message_6.db',
                 datetime(2026, 6, 30), datetime(2026, 8, 23, 23, 59))
    result = ss.scan_decrypted_ranges([str(tmp_path / 'dec')])
    assert result['message_6.db']['start'] == '2026-06-30'
    assert result['message_6.db']['end'] == '2026-08-23'


def test_select_shards_newest_plus_overlap(tmp_path):
    enc = tmp_path / 'enc' / 'message'
    _touch(str(enc), 'message_0.db', 1000)
    _touch(str(enc), 'message_1.db', 2000)
    _touch(str(enc), 'message_2.db', 3000)   # newest
    manifest = {
        'message_0.db': {'start': '2025-06-30', 'end': '2026-06-30'},
        'message_1.db': {'start': '2024-06-30', 'end': '2025-06-30'},
    }
    # 2026-03-01~04-01: message_0 覆盖(2025-06-30~2026-06-30)，message_1 过旧
    sel = ss.select_shards(str(enc), manifest, date(2026, 3, 1), date(2026, 4, 1))
    assert 'message_2.db' in sel      # 最新分片始终包含
    assert 'message_0.db' in sel      # 与区间重叠
    assert 'message_1.db' not in sel  # 过旧


def test_select_shards_backfills_unknown_when_coverage_missing(tmp_path):
    enc = tmp_path / 'enc' / 'message'
    _touch(str(enc), 'message_0.db', 1000)
    _touch(str(enc), 'message_1.db', 2000)   # newest
    manifest = {'message_1.db': {'start': '2026-06-30', 'end': '2026-08-23'}}
    sel = ss.select_shards(str(enc), manifest, date(2026, 1, 1), date(2026, 8, 23))
    assert 'message_1.db' in sel
    assert 'message_0.db' in sel   # 新增未知分片，供回填扫描


def test_select_shards_skips_newest_for_historical_range(tmp_path):
    enc = tmp_path / 'enc' / 'message'
    _touch(str(enc), 'message_6.db', 3000)   # newest (2026)
    _touch(str(enc), 'message_5.db', 2000)
    _touch(str(enc), 'message_4.db', 1000)
    manifest = {
        'message_6.db': {'start': '2026-06-30', 'end': '2026-08-23'},
        'message_5.db': {'start': '2020-07-17', 'end': '2021-07-01'},
        'message_4.db': {'start': '2021-07-01', 'end': '2022-07-01'},
    }
    sel = ss.select_shards(str(enc), manifest, date(2021, 1, 1), date(2021, 12, 31))
    assert 'message_6.db' not in sel   # 纯历史区间不需要最新分片
    assert 'message_5.db' in sel
    assert 'message_4.db' in sel
