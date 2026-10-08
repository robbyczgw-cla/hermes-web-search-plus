"""Public Provider SDK for Web Search Plus.

The implementation lives in ``wsp_core.sdk``. Provider modules in
``providers.d`` keep importing ``wsp_sdk``: during discovery the engine binds
that name to its own SDK object. This package serves code that imports
``wsp_sdk`` from the plugin directory directly, e.g. a provider's own tests.
"""

from wsp_core.sdk import *  # noqa: F401,F403
from wsp_core.sdk import __all__  # noqa: F401
