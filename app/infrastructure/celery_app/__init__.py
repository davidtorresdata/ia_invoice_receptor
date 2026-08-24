"""Async task queue adapters (Celery/Redis).

Submodules are imported directly (`celery_app.app`, `celery_app.tasks`,
`celery_app.dispatcher`) — no eager re-exports here, because tasks import
the composition root (`app.infrastructure.container`) and eager imports
would create a circular dependency at package load time.
"""
