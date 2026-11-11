"""The report UI (9.5, 8.7): read-only pages over the Central Store; actions go to the Reporter service (9.1).

- `ui.app.make_app(store_reader, identity, service=..., artifact_roots=...)`: the WSGI application.
- `ui.pages`: the page renderers, one function per 9.5 page, and the `/a/<sha256>` artifact endpoint.
"""
from .app import HttpServiceClient, ReaderPool, ReportUI, ServiceClient, header_identity, make_app

__all__ = ["HttpServiceClient", "ReaderPool", "ReportUI", "ServiceClient", "header_identity", "make_app"]
