"""Parser tests: bytes in, records or FastaError out. No HTTP, no database."""

from pathlib import Path

import pytest

from api.fasta import MAX_HEADER_LENGTH, MAX_RECORDS, FastaError, Record, parse_fasta

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def rejection(data: bytes) -> FastaError:
    with pytest.raises(FastaError) as caught:
        parse_fasta(data)
    return caught.value


# --- rejected input -----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("empty.fasta", "empty_file"),
        ("whitespace_only.fasta", "empty_file"),
        ("renamed_image.fasta", "not_text"),
        ("not_fasta.fasta", "not_fasta"),
        ("empty_header.fasta", "empty_header"),
        ("header_too_long.fasta", "header_too_long"),
        ("bad_character.fasta", "bad_character"),
        ("protein.fasta", "bad_character"),
        ("alignment_gap.fasta", "bad_character"),
        ("empty_sequence.fasta", "empty_sequence"),
    ],
)
def test_rejected_fixture(name, code):
    assert rejection(fixture(name)).code == code


def test_bad_character_message_names_line_column_and_character():
    message = rejection(fixture("bad_character.fasta")).message
    assert "Line 3, column 5" in message
    assert "'J'" in message


def test_protein_is_caught_at_first_non_nucleotide_letter():
    # M is also an IUPAC nucleotide code (A or C), so E is the first bad one.
    assert "Line 2, column 2: unexpected character 'E'" in rejection(fixture("protein.fasta")).message


def test_empty_sequence_message_names_the_record_line():
    assert "line 3" in rejection(fixture("empty_sequence.fasta")).message


def test_line_numbers_count_blank_lines():
    assert "Line 4, column 2" in rejection(b"\n\n>s\nAJ\n").message


def test_sharp_s_is_rejected_not_uppercased_into_valid_codes():
    # 'ß'.upper() == 'SS', and S is a valid IUPAC code.
    error = rejection(">s\nACGß\n".encode())
    assert error.code == "bad_character"
    assert "'ß'" in error.message


def test_unprintable_character_is_shown_as_code_point():
    assert "U+0007" in rejection(b">s\nAC\x07GT\n").message


def test_non_utf8_text_is_rejected_as_not_text():
    assert rejection(b">s caf\xe9\nACGT\n").code == "not_text"  # Latin-1 é


def test_record_limit_is_exclusive():
    assert len(parse_fasta(b">s\nA\n" * MAX_RECORDS)) == MAX_RECORDS
    assert rejection(b">s\nA\n" * (MAX_RECORDS + 1)).code == "too_many_records"


def test_header_length_limit_is_inclusive():
    at_limit = b">" + b"x" * MAX_HEADER_LENGTH + b"\nACGT\n"
    assert parse_fasta(at_limit)[0].header == "x" * MAX_HEADER_LENGTH
    assert rejection(b">" + b"x" * (MAX_HEADER_LENGTH + 1) + b"\nACGT\n").code == "header_too_long"


# --- accepted input -----------------------------------------------------------


def test_valid_fixture_stats():
    assert parse_fasta(fixture("valid.fasta")) == [
        Record("seq1 test record wrapped over two lines", 32, 8, 8, 8, 8, 0, 0.5),
        # 4 N plus R and Y are "other" and excluded from GC: (3+4)/(4+4+3+3)
        Record("seq2 soft-masked with ambiguity codes", 20, 4, 4, 3, 3, 6, 0.5),
        # U counts as T: (7+5)/(4+5+7+5)
        Record("rna1 RNA fragment", 21, 4, 5, 7, 5, 0, 12 / 21),
    ]


def test_all_n_record_has_no_gc_content():
    unknown, normal = parse_fasta(fixture("all_n.fasta"))
    assert unknown.gc_content is None
    assert unknown.count_other == 10
    assert normal.gc_content == 0.5


def test_header_with_html_is_kept_verbatim():
    # Escaping is the frontend's job (textContent); the parser stores what was sent.
    [record] = parse_fasta(fixture("xss_header.fasta"))
    assert record.header == "seq1 <img src=x onerror=alert('xss')> <b>bold?</b>"


@pytest.mark.parametrize(
    "data",
    [
        b">s\nACGT\nAC\n",  # Unix
        b">s\r\nACGT\r\nAC\r\n",  # Windows
        b">s\rACGT\rAC\r",  # old Mac
        b"\xef\xbb\xbf>s\nACGT\nAC\n",  # UTF-8 byte-order mark (Notepad)
        b"\n\n>s\n\nACGT\n\nAC\n\n",  # blank lines anywhere
        b">s\nAC GT\tAC\n",  # spaces and tabs inside sequence lines
        b">s\nacgt\nac\n",  # lowercase (soft-masked)
    ],
)
def test_lenient_formatting_gives_identical_record(data):
    assert parse_fasta(data) == [Record("s", 6, 2, 2, 1, 1, 0, 0.5)]
