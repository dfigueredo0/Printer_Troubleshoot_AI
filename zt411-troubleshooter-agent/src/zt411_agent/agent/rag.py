"""
Owns: grounding, citation, and doc hygiene.
Retrieves from Zebra manuals/KB + internal KB, dedupes, ranks, and returns snippet IDs.
Maintains offline cache, handles limited connectivity, and blocks prompt injection inside docs.
Provides “what to check next” suggestions tied to doc references and device state.
Evidence: snippet_id, source, section, page/anchor, extracted constraints/warnings.
"""