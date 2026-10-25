import pytest
from open_webui.retrieval.skip_extraction import skip_extraction_reason, skip_extraction_stub


@pytest.mark.parametrize(
    'extension',
    [
        'csv',
        'tsv',
        'tab',
        'xls',
        'xlsx',
        'xlsm',
        'xlsb',
        'xlt',
        'xltx',
        'xltm',
        'xla',
        'xlam',
        'ods',
        'ots',
        'fods',
        'numbers',
    ],
)
def test_spreadsheet_extensions_skip_extraction(extension):
    assert skip_extraction_reason(f'report.{extension.upper()}') == extension


@pytest.mark.parametrize(
    'content_type',
    [
        'text/csv',
        'text/tab-separated-values',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-excel.sheet.macroEnabled.12',
        'application/vnd.ms-excel.sheet.binary.macroEnabled.12',
        'application/vnd.oasis.opendocument.spreadsheet',
        'application/vnd.apple.numbers',
    ],
)
def test_spreadsheet_mime_types_skip_extraction(content_type):
    assert skip_extraction_reason('report', f'{content_type}; charset=utf-8') == content_type.lower()


def test_searchable_document_formats_are_not_skipped():
    assert skip_extraction_reason('report.pdf', 'application/pdf') is None
    assert (
        skip_extraction_reason(
            'report.docx',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )
        is None
    )


def test_skip_stub_describes_structured_data_and_original_blob():
    stub = skip_extraction_stub('report.csv', 'file-id', 'csv')

    assert 'stored as the original blob (id file-id)' in stub
    assert 'structured/tabular data' in stub
    assert 'Use python against the original file.' in stub
