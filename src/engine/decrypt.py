"""
WeChat 4.0 数据库解密器
参数: SQLCipher 4, AES-256-CBC, HMAC-SHA512, reserve=80, page_size=4096
"""
import hashlib
import hmac as hmac_mod
import json
import os
import shutil
import sqlite3
import struct
import sys
from Crypto.Cipher import AES

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'


def derive_mac_key(enc_key, salt):
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def decrypt_page(enc_key, page_data, pgno):
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        page = bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ)
        return bytes(page)
    else:
        encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def decrypt_database(db_path, out_path, enc_key, print_fn=None, progress_fn=None):
    if print_fn is None:
        print_fn = print

    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ
    if file_size % PAGE_SZ != 0:
        total_pages += 1

    # Handle file locking: copy to temp if needed
    tmp_path = None
    actual_path = db_path
    try:
        with open(db_path, 'rb') as fin:
            page1 = fin.read(PAGE_SZ)
    except (PermissionError, OSError):
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp_path = tmp.name
        tmp.close()
        shutil.copy2(db_path, tmp_path)
        actual_path = tmp_path
        with open(actual_path, 'rb') as fin:
            page1 = fin.read(PAGE_SZ)

    if len(page1) < PAGE_SZ:
        print_fn(f"  [ERROR] 文件太小")
        return False

    salt = page1[:SALT_SZ]
    mac_key = derive_mac_key(enc_key, salt)
    p1_hmac_data = page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    p1_stored_hmac = page1[PAGE_SZ - HMAC_SZ: PAGE_SZ]
    hm = hmac_mod.new(mac_key, p1_hmac_data, hashlib.sha512)
    hm.update(struct.pack('<I', 1))
    if hm.digest() != p1_stored_hmac:
        print_fn(f"  [ERROR] Page 1 HMAC验证失败")
        return False

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    last_progress = -1
    try:
        with open(actual_path, 'rb') as fin, open(out_path, 'wb') as fout:
            for pgno in range(1, total_pages + 1):
                page = fin.read(PAGE_SZ)
                if len(page) < PAGE_SZ:
                    if len(page) > 0:
                        page = page + b'\x00' * (PAGE_SZ - len(page))
                    else:
                        break

                decrypted = decrypt_page(enc_key, page, pgno)
                fout.write(decrypted)

                if progress_fn and total_pages > 100:
                    cur = int(pgno / total_pages * 100)
                    if cur > last_progress and cur % 10 == 0:
                        progress_fn(cur, f"{pgno}/{total_pages}")
                        last_progress = cur

        # Verify: at minimum, sqlite_master must be readable
        try:
            conn = sqlite3.connect(out_path)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
            table_count = len(tables)

            # PRAGMA integrity_check is a best-effort check — FTS databases
            # (message_fts.db) with content tables reference external content
            # that doesn't exist standalone, causing SQL logic errors.
            # Don't fail decryption for an integrity_check issue.
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()
                if integrity and integrity[0] != 'ok':
                    print_fn(f"  [WARN] 完整性检查: {integrity[0]}")
            except sqlite3.Error as integ_err:
                print_fn(f"  [WARN] 完整性检查跳过 (FTS/辅助数据库): {integ_err}")

            conn.close()
            for suffix in ("-shm", "-wal"):
                residual = out_path + suffix
                if os.path.exists(residual):
                    try:
                        os.remove(residual)
                    except OSError:
                        pass
            return True
        except (sqlite3.Error, OSError) as e:
            print_fn(f"  [WARN] SQLite验证失败: {e}")
            return False
    except OSError as e:
        print_fn(f"  [ERROR] 写入解密输出失败: {e}")
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def run_decrypt(keys_file=None, db_dir=None, out_dir=None, print_fn=None,
                progress_fn=None, only_rel_paths=None):
    """主入口：批量解密所有数据库。
    Args:
        keys_file: all_keys.json 路径 (None = 从配置文件自动加载)
        db_dir: 原始加密 DB 目录
        out_dir: 解密输出目录
        print_fn: 日志函数
        progress_fn: 进度回调 (pct, msg)
        only_rel_paths: 可选 set，元素为相对 db_dir 的路径（如 'message\\message_6.db'）。
                        非 None 时仅处理命中文件，用于按时间分片精准解密。
    Returns: (success_count, failed_count, skipped_count)
    """
    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    if keys_file is None:
        from engine.config_file import get_db_keys, get_db_dir
        raw_keys = get_db_keys()
        db_dir_val = get_db_dir() or db_dir
        if not raw_keys:
            raise FileNotFoundError("未找到数据库密钥，请先执行密钥提取 (python main.py keys)")
    else:
        if not os.path.exists(keys_file):
            raise FileNotFoundError(f"密钥文件不存在: {keys_file}\n请先执行密钥提取")

        with open(keys_file, encoding="utf-8") as f:
            keys = json.load(f)

        db_dir_val = keys.get("_db_dir", db_dir)
        raw_keys = {}
        for k, v in keys.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict):
                hex_key = v.get("enc_key", "")
                if hex_key and len(hex_key) == 64:
                    raw_keys[k] = hex_key
            elif isinstance(v, str) and len(v) == 64:
                raw_keys[k] = v

    print_fn(f"加载 {len(raw_keys)} 个数据库密钥")
    os.makedirs(out_dir, exist_ok=True)

    # Collect all DB files
    db_files = []
    for root, dirs, files in os.walk(db_dir):
        for f in files:
            if f.endswith('.db') and not f.endswith('-wal') and not f.endswith('-shm'):
                path = os.path.join(root, f)
                rel = os.path.relpath(path, db_dir)
                sz = os.path.getsize(path)
                db_files.append((rel, path, sz))

    db_files.sort(key=lambda x: x[2])

    print_fn(f"找到 {len(db_files)} 个数据库文件\n")

    success = 0
    failed = 0
    skipped = 0
    total = len(db_files)

    for i, (rel, path, sz) in enumerate(db_files):
        if only_rel_paths is not None and rel not in only_rel_paths:
            continue
        pct = 5 + int((i + 1) / total * 90)
        progress_fn(pct, f"解密: {rel}")

        # Match key: try exact path, then HMAC-based trial of all keys
        enc_key_hex = None
        for candidate in [rel, rel.replace("\\", "/"), rel.replace("/", "\\")]:
            if candidate in raw_keys:
                enc_key_hex = raw_keys[candidate]
                break

        if enc_key_hex is None:
            # Try matching by folder/file — prefer same parent dir
            fname = os.path.basename(rel)
            rel_parent = os.path.basename(os.path.dirname(rel))
            best = None
            for k, v in raw_keys.items():
                if os.path.basename(k) == fname:
                    k_parent = os.path.basename(os.path.dirname(k))
                    if rel_parent == k_parent:
                        enc_key_hex = v
                        break
                    if best is None:
                        best = v
            if enc_key_hex is None and best is not None:
                enc_key_hex = best

        # HMAC-based trial: try ALL known keys against page 1
        if enc_key_hex is None and os.path.getsize(path) >= PAGE_SZ:
            try:
                with open(path, 'rb') as f:
                    page1 = f.read(PAGE_SZ)
                for kval in raw_keys.values():
                    if len(kval) == 64:
                        try:
                            kb = bytes.fromhex(kval)
                        except ValueError:
                            continue
                        # Inline HMAC verification (same as key_scan.verify_enc_key)
                        salt = page1[:SALT_SZ]
                        mac_salt = bytes(b ^ 0x3A for b in salt)
                        mac_key = hashlib.pbkdf2_hmac(
                            "sha512", kb, mac_salt, 2, dklen=KEY_SZ)
                        hmac_data = page1[SALT_SZ: PAGE_SZ - 80 + 16]
                        stored_hmac = page1[PAGE_SZ - 64: PAGE_SZ]
                        hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
                        hm.update(struct.pack("<I", 1))
                        if hm.digest() == stored_hmac:
                            enc_key_hex = kval
                            break
            except OSError:
                pass

        if not enc_key_hex:
            print_fn(f"SKIP: {rel} (无密钥)")
            skipped += 1
            continue

        enc_key = bytes.fromhex(enc_key_hex)
        out_path = os.path.join(out_dir, rel)

        print_fn(f"解密: {rel} ({sz/1024/1024:.1f}MB) ...")

        ok = decrypt_database(path, out_path, enc_key, print_fn=print_fn,
                              progress_fn=progress_fn)
        if ok:
            try:
                conn = sqlite3.connect(out_path)
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                conn.close()
                names = [t[0] for t in tables]
                print_fn(f"OK! {len(names)} 表")
                success += 1
            except sqlite3.Error as e:
                print_fn(f"[WARN] {e}")
                failed += 1
        else:
            failed += 1

    progress_fn(98, f"解密完成: {success}成功/{failed}失败/{skipped}跳过")
    return success, failed, skipped
