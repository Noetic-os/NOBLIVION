# SPDX-License-Identifier: AGPL-3.0-or-later
"""``python -m longtest`` runs the commands of ``longtest.cli``."""

import sys

from .cli import main

sys.exit(main())
