from .legacy import *  # noqa: F401,F403  (generated: maps the v2 tables)

try:
    from .extra import *  # noqa: F401,F403  (hand-written additions)
except ImportError:  # no additions yet
    pass
