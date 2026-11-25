"""Fork-only: token counting and token splitting without a Python list of every token.

tiktoken releases the GIL while it encodes, but ``encode()`` then builds a Python
``list[int]`` of every token while holding it, and freeing that list holds it again.
For a large upload (48M tokens for a 120M-char text) that is about 1.7 s plus 0.75 s
of event-loop starvation per call, growing linearly with size, and about 38 bytes of
memory per token. Ingestion counts the whole text once (merge_docs_to_target_size)
and splits it once (TokenTextSplitter), in a worker thread of the serving process.

``encode_to_numpy`` returns the same ids in a uint32 array (4 bytes per token) without
the long GIL holds. NumpyTokenTextSplitter returns exactly what langchain's
TokenTextSplitter returns; its split_text is the loop of langchain-text-splitters 1.1.2
``split_text_on_tokens`` over that array. test/backend/test_ingest_freeze.py compares
both with the installed library; re-diff when upgrading it.
"""

from collections.abc import Collection
from typing import AbstractSet, Literal

from langchain_text_splitters import TokenTextSplitter


def count_tokens(
    encoding,
    text: str,
    disallowed_special: Literal['all'] | Collection[str] = 'all',
) -> int:
    """Return ``len(encoding.encode(text, disallowed_special=...))`` without building the list."""
    return len(encoding.encode_to_numpy(text, disallowed_special=disallowed_special))


class NumpyTokenTextSplitter(TokenTextSplitter):
    """TokenTextSplitter with the same output, holding token ids in a numpy array."""

    def split_text(self, text: str) -> list[str]:
        # Same steps as langchain-text-splitters 1.1.2 split_text_on_tokens.
        allowed_special: Literal['all'] | AbstractSet[str] = self._allowed_special
        input_ids = self._tokenizer.encode_to_numpy(
            text,
            allowed_special=allowed_special,
            disallowed_special=self._disallowed_special,
        )
        tokens_per_chunk = self._chunk_size
        chunk_overlap = self._chunk_overlap
        splits: list[str] = []
        start_idx = 0
        if tokens_per_chunk <= chunk_overlap:
            raise ValueError('tokens_per_chunk must be greater than chunk_overlap')

        total = len(input_ids)
        while start_idx < total:
            cur_idx = min(start_idx + tokens_per_chunk, total)
            chunk_ids = input_ids[start_idx:cur_idx]
            if len(chunk_ids) == 0:
                break
            decoded = self._tokenizer.decode(chunk_ids.tolist())
            if decoded:
                splits.append(decoded)
            if cur_idx == total:
                break
            start_idx += tokens_per_chunk - chunk_overlap
        return splits
