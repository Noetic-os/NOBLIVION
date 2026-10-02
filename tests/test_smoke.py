# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke test: the package imports and has a version."""

import noblivion


def test_package_imports() -> None:
    assert isinstance(noblivion.__version__, str)
    assert noblivion.__version__
