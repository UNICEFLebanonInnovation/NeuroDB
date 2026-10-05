"""Monitoring insights (FMM): the eTools field monitoring visits, how they link to partners, programme
documents, places and sections, and how complete and coherent their reports are.

eTools sends field monitoring through several Datamart datasets whose exact key names were never
documented: the findings (``fm-ontrack``, one row per monitored entity of a visit), the checklist
answers (``fm-questions``), their options and the programme activities of each visit. Nothing here
guesses a key silently. The refresh (:mod:`neurodb.fmm.refresh`) first reads which keys the records
hold (:class:`~neurodb.fmm.models.KeyProbe`) and chooses, for each logical field, the key that fills
it (:class:`~neurodb.fmm.models.FieldMapping`, :mod:`neurodb.fmm.fields`); the admin shows both as
"Fields found". Values are read through :mod:`neurodb.fmm.parse`, which tolerates the shapes the
records may take.

From those records the refresh builds the visits (:mod:`neurodb.fmm.build`): one per eTools monitoring
activity, linked to partners, programme documents, places, sections, offices, action points and
checklist answers, without keeping any narrative or answer text, and scores them with the quality
rules (:mod:`neurodb.fmm.score`). The page (``/fmm/``, :mod:`neurodb.fmm.views`) reads the stored
visits through a filter (:mod:`neurodb.fmm.scope`) and its figures (:mod:`neurodb.fmm.metrics`). The AI
(:mod:`neurodb.fmm.ai`: prompt versions, the sampling guard and the budget so far) stays switched off
until go-live. Other pages read their panels and links from :mod:`neurodb.fmm.services`, and the
knowledge hub its visits from :mod:`neurodb.fmm.hub`. This app imports ``neurodb.datamart`` and
``neurodb.watch``; neither of them, nor ``neurodb.reports`` or ``neurodb.graph``, imports it at module
level.
"""
