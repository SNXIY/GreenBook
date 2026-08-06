"""Dependencies for the assistant API.

In V2, AuthContext is injected via request.state.auth_context
(set by AuthContextResolver or _DevAuthMiddleware).
"""

from __future__ import annotations
