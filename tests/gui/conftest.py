"""Run every GUI test headless.

These are assigned, not defaulted: a desktop session sets QT_QPA_PLATFORM (for example to
"wayland;xcb"), and the tests would then open real windows.
"""

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
# The desktop's Qt theme plugin could otherwise leak its colour scheme into the tests.
os.environ["QT_QPA_PLATFORMTHEME"] = ""
