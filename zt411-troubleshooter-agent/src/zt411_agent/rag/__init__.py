"""
RAG package for the ZT411 troubleshooter.

End-to-end pipeline used by the orchestrator:

    from zt411_agent.rag.retriever import Retriever
    retriever = Retriever()                       # loads index lazily
    snippets = retriever.retrieve(query, k=5)     # list[RagSnippet]

The index itself is built offline by ``index_builder``:

    python -m zt411_agent.rag.index_builder --rebuild
"""

from .retriever import Retriever, retrieve  # noqa: F401
