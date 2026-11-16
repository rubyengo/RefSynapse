"""RefSynapse launcher, compatible with embeddable/portable Python."""
import os
import sys

APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                          else os.path.abspath(__file__))
# Embeddable Python may ignore the script directory through its ._pth file.
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from refsynapse.app import main_entry

if __name__ == "__main__":
    main_entry()
