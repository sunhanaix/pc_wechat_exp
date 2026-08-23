"""Tests for run_decrypt(only_rel_paths=...) — subset decryption filter."""
import json
import os

from engine.decrypt import run_decrypt


def test_run_decrypt_filters_by_rel_paths(tmp_path):
    db_dir = tmp_path / 'db'
    (db_dir / 'message').mkdir(parents=True)
    (db_dir / 'contact').mkdir(parents=True)
    (db_dir / 'message' / 'message_6.db').write_bytes(b'\x00' * 4096)
    (db_dir / 'contact' / 'contact.db').write_bytes(b'\x00' * 4096)

    out = tmp_path / 'out'
    keys_file = tmp_path / 'keys.json'
    keys_file.write_text(json.dumps({'_db_dir': str(db_dir)}), encoding='utf-8')

    logs = []
    run_decrypt(keys_file=str(keys_file), db_dir=str(db_dir), out_dir=str(out),
                print_fn=logs.append, progress_fn=lambda p, m: None,
                only_rel_paths={os.path.join('message', 'message_6.db')})

    joined = '\n'.join(logs)
    assert 'message_6.db' in joined           # in-set: 进入尝试（无密钥→SKIP）
    assert 'contact.db' not in joined         # out-set: 完全不处理
    assert not (out / 'contact' / 'contact.db').exists()
