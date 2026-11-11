import os
import sys

# Tests import the worker as `worker.*`, the same way `python3 -m worker.main` does from server/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
