"""`python -m annotate`: what the plugin's `bin/annotate` and the systemd unit
run (runtime.py)."""
import sys

from .cli import main

sys.exit(main())
