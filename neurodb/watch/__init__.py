"""NeuroDB Watch: the "For you" assistant that works in the background without being asked.

Every morning (and shortly after new data arrives) it asks fixed questions of NeuroDB's own tables:
what is due soon, what needs someone, how open points connect. It remembers what it found and what
each person was told, so nobody is told the same thing twice without a reason. It writes only its own
tables, the AI use ledger and its own ``SyncRun``.
"""
