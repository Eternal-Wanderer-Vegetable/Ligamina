"""Test-driver preload that seeds the shared Python RNG before the frozen plugin."""

from __future__ import annotations

import os
import random


seed = os.environ.get("LIGAMINA_BEHAVIOR_RANDOM_SEED")
if seed is None or not seed.isdecimal():
    raise RuntimeError("Phase 4 deterministic fixture requires a decimal random seed")
random.seed(int(seed))
