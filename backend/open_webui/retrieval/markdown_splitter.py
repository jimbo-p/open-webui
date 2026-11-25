"""Fork-only: a linear-time MarkdownHeaderTextSplitter for document ingestion.

Ingestion splits text in a worker thread of the serving process, so any step that
holds the GIL for long, or grows quadratically, starves the event loop and the task
fails its health check. langchain-text-splitters 1.1.2 (pinned in requirements.txt)
has two such steps in MarkdownHeaderTextSplitter:

- aggregate_lines_to_chunks grows each chunk with ``+=`` on a dict value, which
  copies the whole chunk for every blank-line block (quadratic in blocks);
- split_text splits the whole text in one call and filters non-printable characters
  with one C-level join per line, which holds the GIL for seconds on one long line.

This subclass returns the same Documents as upstream for the same settings. Its
split_text is a copy of upstream 1.1.2 with only the line iteration and the
printable filter replaced by bounded steps. test/backend/test_ingest_freeze.py
compares it with the installed upstream class; re-diff when upgrading the library.
"""

from collections.abc import Iterator

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter

# Most characters one C-level string call handles here, so the GIL is released
# between steps (about 1-40 ms each).
STEP_CHARS = 1 << 20


def iter_lines(text: str) -> Iterator[str]:
    """Yield the items of ``text.split('\\n')``, about STEP_CHARS characters at a time."""
    start, end = 0, len(text)
    while start + STEP_CHARS < end:
        cut = text.find('\n', start + STEP_CHARS)
        if cut < 0:
            break
        yield from text[start:cut].split('\n')
        start = cut + 1
    yield from text[start:].split('\n')


def keep_printable(text: str) -> str:
    """Return ``''.join(filter(str.isprintable, text))``, in steps of STEP_CHARS."""
    if len(text) <= STEP_CHARS:
        return ''.join(filter(str.isprintable, text))
    parts = []
    for start in range(0, len(text), STEP_CHARS):
        part = text[start : start + STEP_CHARS]
        parts.append(part if part.isprintable() else ''.join(filter(str.isprintable, part)))
    return ''.join(parts)


class LinearMarkdownHeaderTextSplitter(MarkdownHeaderTextSplitter):
    """MarkdownHeaderTextSplitter with the same output, in linear time and bounded GIL holds."""

    def aggregate_lines_to_chunks(self, lines: list[dict]) -> list[Document]:
        # Same rules as upstream 1.1.2, but each chunk collects its parts and is joined once.
        # The last line of '  \n'.join(parts) is the last line of parts[-1].
        parts: list[list[str]] = []
        metadatas: list[dict[str, str]] = []
        for line in lines:
            if metadatas and metadatas[-1] == line['metadata']:
                parts[-1].append(line['content'])
            elif (
                metadatas
                and metadatas[-1] != line['metadata']
                and len(metadatas[-1]) < len(line['metadata'])
                # upstream: content.split('\n')[-1][0] == '#', IndexError included
                and parts[-1][-1][parts[-1][-1].rfind('\n') + 1] == '#'
                and not self.strip_headers
            ):
                parts[-1].append(line['content'])
                metadatas[-1] = line['metadata']
            else:
                parts.append([line['content']])
                metadatas.append(line['metadata'])

        return [
            Document(page_content='  \n'.join(chunk_parts), metadata=metadata)
            for chunk_parts, metadata in zip(parts, metadatas)
        ]

    def split_text(self, text: str) -> list[Document]:
        # Copy of langchain-text-splitters 1.1.2 MarkdownHeaderTextSplitter.split_text.
        # Only iter_lines() (was text.split('\n')) and keep_printable() (was
        # ''.join(filter(str.isprintable, ...))) differ; both give identical results.
        lines_with_metadata: list[dict] = []
        current_content: list[str] = []
        current_metadata: dict[str, str] = {}
        header_stack: list[dict] = []
        initial_metadata: dict[str, str] = {}
        in_code_block = False
        opening_fence = ''

        for line in iter_lines(text):
            stripped_line = keep_printable(line.strip())
            if not in_code_block:
                # Exclude inline code spans
                if stripped_line.startswith('```') and stripped_line.count('```') == 1:
                    in_code_block = True
                    opening_fence = '```'
                elif stripped_line.startswith('~~~'):
                    in_code_block = True
                    opening_fence = '~~~'
            elif stripped_line.startswith(opening_fence):
                in_code_block = False
                opening_fence = ''

            if in_code_block:
                current_content.append(stripped_line)
                continue

            for sep, name in self.headers_to_split_on:
                is_standard_header = stripped_line.startswith(sep) and (
                    len(stripped_line) == len(sep) or stripped_line[len(sep)] == ' '
                )
                is_custom_header = self._is_custom_header(stripped_line, sep)

                if is_standard_header or is_custom_header:
                    if name is not None:
                        if sep in self.custom_header_patterns:
                            current_header_level = self.custom_header_patterns[sep]
                        else:
                            current_header_level = sep.count('#')

                        while header_stack and header_stack[-1]['level'] >= current_header_level:
                            popped_header = header_stack.pop()
                            if popped_header['name'] in initial_metadata:
                                initial_metadata.pop(popped_header['name'])

                        if is_custom_header:
                            header_text = stripped_line[len(sep) : -len(sep)].strip()
                        else:
                            header_text = stripped_line[len(sep) :].strip()

                        header_stack.append({'level': current_header_level, 'name': name, 'data': header_text})
                        initial_metadata[name] = header_text

                    if current_content:
                        lines_with_metadata.append(
                            {'content': '\n'.join(current_content), 'metadata': current_metadata.copy()}
                        )
                        current_content.clear()

                    if not self.strip_headers:
                        current_content.append(stripped_line)

                    break
            else:
                if stripped_line:
                    current_content.append(stripped_line)
                elif current_content:
                    lines_with_metadata.append(
                        {'content': '\n'.join(current_content), 'metadata': current_metadata.copy()}
                    )
                    current_content.clear()

            current_metadata = initial_metadata.copy()

        if current_content:
            lines_with_metadata.append({'content': '\n'.join(current_content), 'metadata': current_metadata})

        if not self.return_each_line:
            return self.aggregate_lines_to_chunks(lines_with_metadata)
        return [Document(page_content=chunk['content'], metadata=chunk['metadata']) for chunk in lines_with_metadata]
