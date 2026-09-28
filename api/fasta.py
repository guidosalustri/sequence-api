"""Parse and validate FASTA uploads.

Pure: bytes in, records out. No HTTP or database code, so it can be tested
on its own. Every rejection raises FastaError with a stable code and a
message meant for the person who uploaded the file.
"""

from dataclasses import dataclass

MAX_HEADER_LENGTH = 1000
MAX_RECORDS = 10_000

# IUPAC nucleotide codes: ACGT, U (RNA), N (any base), and the ambiguity
# codes for two or three possible bases. Both cases are allowed; lowercase
# commonly marks soft-masked regions.
NUCLEOTIDES = "ACGTURYSWKMBDHVN"
ALLOWED = frozenset(NUCLEOTIDES + NUCLEOTIDES.lower())


class FastaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Record:
    header: str
    length: int
    count_a: int
    count_c: int
    count_g: int
    count_t: int  # includes U
    count_other: int  # N and other ambiguity codes
    gc_content: float | None  # fraction 0-1; None if no unambiguous bases


def parse_fasta(data: bytes) -> list[Record]:
    if b"\x00" in data:
        raise _not_text()
    try:
        text = data.decode("utf-8-sig")  # -sig strips a byte-order mark
    except UnicodeDecodeError:
        raise _not_text() from None
    if not text.strip():
        raise FastaError("empty_file", "The file is empty.")

    records: list[Record] = []
    header: str | None = None
    header_line = 0
    chunks: list[str] = []

    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith(">"):
            if header is not None:
                records.append(_make_record(header, header_line, chunks))
            if len(records) >= MAX_RECORDS:
                raise FastaError(
                    "too_many_records",
                    f"The file has more than {MAX_RECORDS:,} records.",
                )
            header = stripped[1:].strip()
            if not header:
                raise FastaError(
                    "empty_header", f"Line {line_no}: header has no text after '>'."
                )
            if len(header) > MAX_HEADER_LENGTH:
                raise FastaError(
                    "header_too_long",
                    f"Line {line_no}: header is {len(header):,} characters; "
                    f"the limit is {MAX_HEADER_LENGTH:,}.",
                )
            header_line = line_no
            chunks = []

        elif header is None:
            raise FastaError(
                "not_fasta",
                f"Line {line_no} should be a header starting with '>'. "
                "Is this a FASTA file?",
            )

        else:
            bases = "".join(line.split())  # spaces and tabs carry no information
            if not set(bases) <= ALLOWED:
                raise _bad_character(line_no, line)
            chunks.append(bases.upper())  # safe: only ASCII letters remain

    records.append(_make_record(header, header_line, chunks))
    return records


def _make_record(header: str, header_line: int, chunks: list[str]) -> Record:
    if not chunks:
        raise FastaError(
            "empty_sequence", f"The record starting on line {header_line} has no sequence."
        )
    seq = "".join(chunks)
    a, c, g = seq.count("A"), seq.count("C"), seq.count("G")
    t = seq.count("T") + seq.count("U")
    unambiguous = a + c + g + t
    return Record(
        header=header,
        length=len(seq),
        count_a=a,
        count_c=c,
        count_g=g,
        count_t=t,
        count_other=len(seq) - unambiguous,
        gc_content=(g + c) / unambiguous if unambiguous else None,
    )


def _bad_character(line_no: int, line: str) -> FastaError:
    # Only reached once we know the line is bad, so the slow scan is fine here.
    for col, ch in enumerate(line, start=1):
        if not ch.isspace() and ch not in ALLOWED:
            shown = f"'{ch}'" if ch.isprintable() else f"U+{ord(ch):04X}"
            return FastaError(
                "bad_character",
                f"Line {line_no}, column {col}: unexpected character {shown}. "
                "Only nucleotide sequences are supported "
                "(A, C, G, T/U, N and IUPAC codes).",
            )
    raise AssertionError("unreachable: line was rejected but has no bad character")


def _not_text() -> FastaError:
    return FastaError(
        "not_text",
        "This doesn't look like a UTF-8 text file. Upload a plain-text FASTA "
        "file (if it has accented characters, save it as UTF-8 first).",
    )
