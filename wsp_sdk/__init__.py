"""Public Provider SDK for Web Search Plus.

The implementation lives in ``wsp_core.sdk``. Provider modules in
``providers.d`` keep importing ``wsp_sdk``: during discovery the engine binds
that name to its own SDK. This package serves code that imports ``wsp_sdk``
from the plugin directory directly, e.g. a provider's own tests; it binds the
same name (and ``wsp_sdk.api``, ``.errors``, ``.conformance``) to the very
modules the engine uses, so there is one class object per name in a process.
"""

from wsp_core import sdk as _sdk
from wsp_core.sdk import *  # noqa: F401,F403
from wsp_core.sdk import __all__  # noqa: F401

_sdk._bind_public_name(__name__)
