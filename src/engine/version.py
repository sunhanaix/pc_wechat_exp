"""Version information for WeChat EXP.

Version format: MAJOR.MINOR.YYYYMMDD (e.g. 2.1.20260529)
- MAJOR.MINOR are hardcoded and bumped manually.
- DATE is auto-derived from git last commit date, or SOURCE_DATE_EPOCH env var
  (for reproducible builds), falling back to today's date.
"""

import os as _os
import subprocess as _subprocess
from datetime import date as _date, datetime as _datetime, timezone as _timezone

MAJOR = 2
MINOR = 12


def _get_build_date() -> str:
    """Auto-derive build date string (YYYYMMDD)."""
    # Reproducible builds: respect SOURCE_DATE_EPOCH
    epoch = _os.environ.get('SOURCE_DATE_EPOCH', '')
    if epoch and epoch.isdigit():
        return _datetime.fromtimestamp(int(epoch), tz=_timezone.utc).strftime('%Y%m%d')

    # Try git last commit date. If the working tree has uncommitted changes,
    # use today's date instead — the in-progress code is newer than the last commit.
    try:
        repo_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        result = _subprocess.run(
            ['git', 'log', '-1', '--format=%ct'],
            capture_output=True, text=True, timeout=5,
            cwd=repo_root,
        )
        if result.returncode == 0 and result.stdout.strip():
            ts = int(result.stdout.strip())
            # Check for uncommitted changes (working tree dirty)
            status = _subprocess.run(
                ['git', 'status', '--porcelain'],
                capture_output=True, text=True, timeout=5,
                cwd=repo_root,
            )
            if status.returncode == 0 and status.stdout.strip():
                # Working tree is dirty — use today's date
                return _date.today().strftime('%Y%m%d')
            return _datetime.fromtimestamp(ts, tz=_timezone.utc).strftime('%Y%m%d')
    except Exception:
        pass

    return _date.today().strftime('%Y%m%d')


BUILD_DATE = _get_build_date()
VERSION = f'{MAJOR}.{MINOR}.{BUILD_DATE}'
VERSION_TUPLE = (MAJOR, MINOR, BUILD_DATE)


def get_version() -> str:
    """Return the full version string."""
    return VERSION
