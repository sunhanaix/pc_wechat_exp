"""Time-based message shard selection for targeted decryption.

WeChat 4.0 stores messages in time-sharded DBs (message_0.db .. message_6.db).
A weekly report only needs shards overlapping its date range. This module keeps
a manifest of each shard's [start, end] (scanned from decrypted copies) and
selects the minimal set of shards to decrypt for a given date range.
"""
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

_SHARD_RE = re.compile(r'message_(\d+)\.db$')


def list_shards(msg_dir):
    """Return message_*.db filenames, newest first (by mtime)."""
    try:
        files = [f for f in os.listdir(msg_dir) if _SHARD_RE.match(f)]
    except OSError:
        return []
    files.sort(key=lambda f: os.path.getmtime(os.path.join(msg_dir, f)), reverse=True)
    return files


def _scan_one_db(path):
    """Return {start, end, msgs} from a decrypted shard, or None."""
    conn = None
    try:
        conn = sqlite3.connect(path)
        tables = [t[0] for t in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        if not tables:
            return None
        mn = mx = None
        total = 0
        for t in tables:
            r = conn.execute(
                "SELECT MIN(create_time), MAX(create_time), COUNT(*) FROM [%s]" % t
            ).fetchone()
            if r and r[2]:
                total += r[2]
                if r[0] and (mn is None or r[0] < mn):
                    mn = r[0]
                if r[1] and (mx is None or r[1] > mx):
                    mx = r[1]
        if mn is None or mx is None:
            return None
        fmt = lambda ts: datetime.fromtimestamp(ts).date().isoformat()
        return {"start": fmt(mn), "end": fmt(mx), "msgs": total}
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def scan_decrypted_ranges(decrypted_dirs, print_fn=None):
    """Scan decrypted dirs' message/ folder → {shard: {start, end, msgs}}."""
    result = {}
    for d in decrypted_dirs:
        msg_dir = os.path.join(d, 'message')
        if not os.path.isdir(msg_dir):
            continue
        for f in list_shards(msg_dir):
            info = _scan_one_db(os.path.join(msg_dir, f))
            if info:
                result[f] = info
    return result


def load_manifest(path):
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except (ValueError, OSError):
            pass
    return {}


def save_manifest(path, manifest):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def merge_manifest(manifest, scan):
    merged = dict(manifest)
    merged.update(scan)
    return merged


def _overlaps(entry, date_from, date_to, margin_days):
    start = date.fromisoformat(entry['start'])
    end = date.fromisoformat(entry['end'])
    lo = date_from - timedelta(days=margin_days)
    hi = date_to + timedelta(days=margin_days)
    return not (end < lo or start > hi)


def covered_until(selected, manifest):
    """Earliest known start among selected shards, or None."""
    starts = [date.fromisoformat(manifest[f]['start'])
              for f in selected if manifest.get(f, {}).get('start')]
    return min(starts) if starts else None


def select_shards(encrypted_msg_dir, manifest, date_from, date_to, margin_days=1):
    """Minimal list of message_x.db to decrypt for the date range.

    Always includes the newest shard (it contains current writes / date_to),
    every known shard whose range overlaps [date_from, date_to] (±margin),
    and — when known coverage doesn't reach date_from — the newest *unknown*
    shard as a scan candidate so the caller can decrypt it, learn its range
    and re-run (first-run / new-shard backfill).
    """
    present = list_shards(encrypted_msg_dir)
    if not present:
        return []

    selected = {present[0]}                       # newest shard always needed
    for f in present:
        entry = manifest.get(f)
        if entry and _overlaps(entry, date_from, date_to, margin_days):
            selected.add(f)

    cu = covered_until(selected, manifest)
    if cu is None or cu > date_from:
        for f in present:                         # backfill: newest unknown shard
            if f not in selected and f not in manifest:
                selected.add(f)
                break
    return sorted(selected)
