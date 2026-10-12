import shutil
import subprocess
from pathlib import Path
import pytest


def test_push_preserves_filters_page_scroll_and_pending_user_requests():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for frontend execution checks')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / 'tests/js/live_messages_guard.js'),
                             str(root / 'src/web/static/js/live-messages.js')],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
