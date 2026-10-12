"""litellm must be fully imported by the time the API module is, not on the
first request (litellm 1.105 raised KeyError: 'litellm' from a log filter when
its first import happened mid-request)."""
import subprocess
import sys


def test_api_main_imports_litellm_eagerly():
    code = (
        "import sys; assert 'litellm' not in sys.modules; "
        "import src.api.main; assert 'litellm' in sys.modules"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, r.stderr[-800:]
