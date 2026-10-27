# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Compatibility import for applications using the original package name."""

import necko

__all__ = necko.__all__
__version__ = necko.__version__
globals().update({name: getattr(necko, name) for name in __all__})
