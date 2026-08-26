"""Vector indexing layer — code embeddings + Qdrant.

Modules:
    chunker.py        — LlamaIndex CodeSplitter wrapper for tree-sitter chunking
                        (test-only today: its last production caller went with
                        qdrant_indexer.py; kept because it is one of the four
                        surfaces the supported-language list must agree across)
    embedder.py       — the Embedder protocol and OpenAICompatibleEmbedder
                        (Ollama/vLLM/llama.cpp/TEI/Infinity — anything serving
                        POST /v1/embeddings). Gemini embeddings do NOT come
                        from here; they go through src.llm.completion.embed.

Reused in Mode A (vault embeddings) and Mode B (semantic chunk retrieval).
"""

from src.indexing.vectors.embedder import (
    Embedder,
    EmbeddingResult,
    OpenAICompatibleEmbedder,
    get_embedder,
)

__all__ = [
    "Embedder",
    "EmbeddingResult",
    "OpenAICompatibleEmbedder",
    "get_embedder",
]
