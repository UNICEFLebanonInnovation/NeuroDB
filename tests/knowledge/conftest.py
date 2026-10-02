"""Fixtures shared with the periodic report tests: the knowledge base's (an editor, file storage,
started background commands) and the assistant's scripted model."""

from tests.assistant.test_assistant import fake  # noqa: F401
from tests.knowledge.test_knowledge import editor, media, records, started  # noqa: F401
