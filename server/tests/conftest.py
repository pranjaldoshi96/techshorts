import os
import sys

# Tests import the worker as `worker.*`, the same way `python3 -m worker.main` does from server/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# config.yaml names the live repository as publish.git_remote. A test run must never push there, whatever the
# shell it was started from had set (worker/mirror.py pushes only with TECHGIST_MIRROR=1).
os.environ.pop("TECHGIST_MIRROR", None)
