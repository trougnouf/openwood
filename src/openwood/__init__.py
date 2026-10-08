"""openwood: control a Charnwood E-series stove (e.g. Aire 300) from Linux.

Protocol reverse engineered from the Charnwood-E Android app v2.0.31.
"""

__version__ = "0.1.0"

from .client import Stove
from .protocol import StoveState

__all__ = ["Stove", "StoveState"]
