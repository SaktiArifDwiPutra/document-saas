"""Strict upload validation and archive/XML safety checks (Milestone M5)."""

import contextlib
import hashlib
import io
import re
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from app.core.errors import AppError

AllowedUploadKind = Literal["docx", "pdf", "txt"]

ALLOWED_MIME_BY_KIND: dict[AllowedUploadKind, frozenset[str]] = {
    "docx": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    ),
    "pdf": frozenset({"application/pdf"}),
    "txt": frozenset({"text/plain"}),
}

_DISALLOWED_DOCX_DIR_PARTS: frozenset[str] = frozenset({"activex", "embeddings", "macros"})
_DISALLOWED_DOCX_STEM_PREFIXES: tuple[str, ...] = (
    "vbaproject",
    "vbadata",
    "oleobject",
    "activex",
)
_DISALLOWED_DOCX_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".bin",
        ".ole",
        ".exe",
        ".dll",
        ".vbs",
        ".js",
        ".ps1",
        ".bat",
        ".cmd",
        ".scr",
        ".com",
        ".jar",
        ".hta",
        ".wsf",
        ".docm",
        ".dotm",
        ".xlsm",
    }
)
_PRINTER_SETTINGS_PATH_RE = re.compile(r"^word/printersettings/printersettings\d*\.bin$")
_PRINTER_SETTINGS_MAX_BYTES = 262_144
_DISALLOWED_PRINTER_SETTINGS_MAGIC: tuple[bytes, ...] = (
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",  # OLE2 Compound Document
    b"MZ",  # PE / DOS executable
    b"PK\x03\x04",  # Embedded ZIP archive
    b"#!",  # Script shebang
)
_HYPERLINK_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
)
_SMALL_DOCX_UNCOMPRESSED_FLOOR_BYTES = 64 * 1024
_DISALLOWED_CONTENT_TYPE_SUBSTRINGS = (
    b"macroenabled",
    b"oleobject",
    b"activex",
)
_XML_DOCTYPE_OR_ENTITY_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_PDF_ACTIVE_CONTENT_RE = re.compile(
    rb"/(?:JavaScript|JS|Launch|EmbeddedFile|OpenAction|RichMedia)(?=[\s/<(\[])",
    re.IGNORECASE,
)
_PDF_ENCRYPT_RE = re.compile(rb"/Encrypt(?=[\s/<(\[])", re.IGNORECASE)
_PDF_DELIMITERS = frozenset(b"()<>[]{}/%")
_PDF_WHITESPACE = frozenset(b" \t\r\n\x00\x0c")
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class ValidatedUploadResult:
    """Normalized summary and content hash of a validated uploaded source."""

    kind: AllowedUploadKind
    content_type: str
    size_bytes: int
    content_hash: str
    extracted_text: str
    metadata: dict[str, object]


def sanitize_filename(filename: str) -> str:
    """Return a clean basename safe for object key path segments."""
    base = PurePosixPath(filename.replace("\\", "/")).name.strip()
    cleaned = _FILENAME_SAFE_RE.sub("_", base).strip("._")
    if not cleaned:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Nama file unggahan tidak valid.",
            status_code=422,
        )
    return cleaned[:120]


def normalize_content_type(content_type: str) -> str:
    """Normalize MIME type header by dropping parameters and lowercasing."""
    return content_type.split(";", 1)[0].strip().lower()


def validate_upload_declaration(
    *,
    filename: str,
    content_type: str,
    size_bytes: int,
    max_file_size_bytes: int,
) -> tuple[AllowedUploadKind, str, str]:
    """Validate extension, MIME alignment, and declared size before issuing upload intent."""
    clean_name = sanitize_filename(filename)
    ext = PurePosixPath(clean_name).suffix.lower()
    if ext not in (".docx", ".pdf", ".txt"):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Format file tidak didukung. Gunakan DOCX, PDF, atau TXT.",
            status_code=422,
        )
    kind: AllowedUploadKind = ext[1:]  # type: ignore[assignment]
    normalized_mime = normalize_content_type(content_type)
    if normalized_mime not in ALLOWED_MIME_BY_KIND[kind]:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Tipe MIME file tidak sesuai dengan ekstensi file yang diunggah.",
            status_code=422,
        )
    if size_bytes <= 0 or size_bytes > max_file_size_bytes:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Ukuran file melebihi batas maksimum atau file kosong.",
            status_code=422,
        )
    return kind, normalized_mime, clean_name


def _parse_xml_safely(raw_xml: bytes, entry_name: str) -> ET.Element:
    """Reject DTD/entity declarations and parse XML safely using stdlib ElementTree."""
    if _XML_DOCTYPE_OR_ENTITY_RE.search(raw_xml):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Konten XML mengandung DTD atau entitas eksternal yang ditolak.",
            status_code=422,
            details={"entry": entry_name},
        )
    try:
        return ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Struktur XML file DOCX rusak atau tidak valid.",
            status_code=422,
            details={"entry": entry_name},
        ) from exc


def extract_docx_paragraphs(document_xml: bytes) -> list[str]:
    """Extract paragraph strings from word/document.xml, joining split Word XML runs."""
    root = _parse_xml_safely(document_xml, "word/document.xml")
    paragraphs: list[str] = []
    p_tag = f"{{{_W_NS}}}p"
    t_tag = f"{{{_W_NS}}}t"
    for p_elem in root.iter(p_tag):
        parts = [t_elem.text for t_elem in p_elem.iter(t_tag) if t_elem.text]
        joined = "".join(parts).strip()
        if joined:
            paragraphs.append(joined)
    return paragraphs


def _check_docx_entry_name(normalized_entry: str) -> bool:
    """Validate an archive entry path and return True if it is a candidate printerSettings bin."""
    lowered = normalized_entry.lower()
    path = PurePosixPath(lowered)
    if any(part in _DISALLOWED_DOCX_DIR_PARTS for part in path.parts[:-1]):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Konten makro, biner, atau objek aktif tersemat ditolak.",
            status_code=422,
        )
    if path.name.startswith(_DISALLOWED_DOCX_STEM_PREFIXES):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Konten makro, biner, atau objek aktif tersemat ditolak.",
            status_code=422,
        )
    if path.suffix in _DISALLOWED_DOCX_EXTENSIONS:
        if _PRINTER_SETTINGS_PATH_RE.match(lowered):
            return True
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Konten makro, biner, atau objek aktif tersemat ditolak.",
            status_code=422,
        )
    return False


def _content_types_declares_printer_settings(content_types_root: ET.Element) -> bool:
    """Return True if parsed [Content_Types].xml declares Word printerSettings MIME type."""
    for elem in content_types_root.iter():
        ct = elem.attrib.get("ContentType", "").strip().lower()
        if ct == "application/vnd.openxmlformats-officedocument.wordprocessingml.printersettings":
            return True
    return False


def _validate_printer_settings_bin(
    entry_name: str,
    chunk: bytes,
    *,
    has_printer_settings_content_type: bool,
) -> None:
    """Ensure a word/printerSettings/*.bin entry matches expected Word metadata and safe bytes."""
    if (
        not _PRINTER_SETTINGS_PATH_RE.match(entry_name.lower())
        or not has_printer_settings_content_type
        or len(chunk) == 0
        or len(chunk) > _PRINTER_SETTINGS_MAX_BYTES
    ):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File pengaturan printer DOCX tidak valid atau tidak sesuai konteks.",
            status_code=422,
            details={"entry": entry_name},
        )
    if any(chunk.startswith(sig) for sig in _DISALLOWED_PRINTER_SETTINGS_MAGIC):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Konten biner berbahaya terdeteksi pada pengaturan printer DOCX.",
            status_code=422,
            details={"entry": entry_name},
        )
    lowered_bytes = chunk.lower()
    if b"vbaproject" in lowered_bytes or b"<script" in lowered_bytes:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Konten skrip atau makro terdeteksi pada pengaturan printer DOCX.",
            status_code=422,
            details={"entry": entry_name},
        )


def _is_allowed_external_relationship(elem: ET.Element) -> bool:
    """Allow only passive HTTP, HTTPS, or mailto hyperlink external rels (never fetched)."""
    rel_type = elem.attrib.get("Type", "").strip()
    if rel_type != _HYPERLINK_REL_TYPE:
        return False
    target = elem.attrib.get("Target", "").strip()
    if not target or len(target) > 2048 or any(ord(ch) <= 32 for ch in target):
        return False
    if target.startswith("//") or target.startswith("\\\\"):
        return False

    parsed = urlsplit(target)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        return bool(parsed.netloc and parsed.hostname)
    if scheme == "mailto":
        local_part, _, domain_part = parsed.path.partition("@")
        return not parsed.netloc and bool(local_part and domain_part and "." in domain_part)
    return False


def _validate_docx_bytes(
    data: bytes,
    *,
    max_file_size_bytes: int,
    max_archive_expansion_ratio: int,
    max_zip_entries: int,
) -> tuple[str, dict[str, object]]:
    """Validate DOCX ZIP structure, bomb limits, active content, DTDs, and external refs."""
    if not data.startswith(b"PK\x03\x04"):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Magic bytes file DOCX tidak valid.",
            status_code=422,
        )

    max_uncompressed_bytes = max_file_size_bytes * max_archive_expansion_ratio
    small_file_floor = min(_SMALL_DOCX_UNCOMPRESSED_FLOOR_BYTES, max_file_size_bytes)
    max_ratio_bytes = max(len(data), 1) * max_archive_expansion_ratio
    effective_uncompressed_cap = min(
        max_uncompressed_bytes,
        max(max_ratio_bytes, small_file_floor),
    )

    try:
        with zipfile.ZipFile(io.BytesIO(data), mode="r") as zf:
            infos = zf.infolist()
            if len(infos) > max_zip_entries or len(infos) == 0:
                raise AppError(
                    code="UPLOAD_REJECTED",
                    message="Jumlah entri arsip DOCX melebihi batas aman.",
                    status_code=422,
                )

            seen_names: set[str] = set()
            total_actual_uncompressed = 0
            entries_data: dict[str, bytes] = {}
            printer_settings_entries: list[str] = []

            for info in infos:
                normalized_entry = info.filename.replace("\\", "/")
                if (
                    normalized_entry.startswith("/")
                    or ".." in PurePosixPath(normalized_entry).parts
                    or ((info.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise AppError(
                        code="UPLOAD_REJECTED",
                        message="Entri path berbahaya terdeteksi di dalam file DOCX.",
                        status_code=422,
                    )
                if normalized_entry in seen_names:
                    raise AppError(
                        code="UPLOAD_REJECTED",
                        message="Entri duplikat terdeteksi di dalam arsip DOCX.",
                        status_code=422,
                    )
                seen_names.add(normalized_entry)

                if _check_docx_entry_name(normalized_entry):
                    printer_settings_entries.append(normalized_entry)

                with zf.open(info, mode="r") as member:
                    remaining = effective_uncompressed_cap - total_actual_uncompressed + 1
                    chunk = member.read(remaining)
                    total_actual_uncompressed += len(chunk)
                    if total_actual_uncompressed > effective_uncompressed_cap:
                        raise AppError(
                            code="UPLOAD_REJECTED",
                            message="Rasio ekspansi arsip DOCX melebihi batas aman.",
                            status_code=422,
                        )
                    entries_data[normalized_entry] = chunk
    except (zipfile.BadZipFile, RuntimeError, ValueError, OSError) as exc:
        if isinstance(exc, AppError):
            raise
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File DOCX rusak atau bukan arsip ZIP yang valid.",
            status_code=422,
        ) from exc

    if "[Content_Types].xml" not in entries_data or "word/document.xml" not in entries_data:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File DOCX tidak memiliki struktur dokumen Word yang valid.",
            status_code=422,
        )

    content_types_lower = entries_data["[Content_Types].xml"].lower()
    if any(marker in content_types_lower for marker in _DISALLOWED_CONTENT_TYPE_SUBSTRINGS):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="Tipe konten makro atau ActiveX di dalam DOCX ditolak.",
            status_code=422,
        )

    content_types_root = _parse_xml_safely(
        entries_data["[Content_Types].xml"],
        "[Content_Types].xml",
    )
    has_printer_settings_ct = _content_types_declares_printer_settings(content_types_root)
    for ps_entry in printer_settings_entries:
        _validate_printer_settings_bin(
            ps_entry,
            entries_data[ps_entry],
            has_printer_settings_content_type=has_printer_settings_ct,
        )

    for entry_name, raw in entries_data.items():
        lowered = entry_name.lower()
        if entry_name == "[Content_Types].xml":
            continue
        if lowered.endswith(".xml") or lowered.endswith(".rels"):
            root = _parse_xml_safely(raw, entry_name)
            if lowered.endswith(".rels"):
                for elem in root.iter():
                    if (
                        elem.attrib.get("TargetMode", "").strip().lower() == "external"
                        and not _is_allowed_external_relationship(elem)
                    ):
                        raise AppError(
                            code="UPLOAD_REJECTED",
                            message="Referensi eksternal di dalam relasi DOCX tidak diizinkan.",
                            status_code=422,
                            details={"entry": entry_name},
                        )

    paragraphs = extract_docx_paragraphs(entries_data["word/document.xml"])
    extracted_text = "\n".join(paragraphs).strip()
    return extracted_text, {
        "entry_count": len(entries_data),
        "uncompressed_bytes": total_actual_uncompressed,
        "paragraph_count": len(paragraphs),
    }


def _read_pdf_literal_string(buf: bytes, start: int) -> tuple[str, int]:
    """Parse a PDF (...) literal string starting at index `start`, handling escapes and nesting."""
    out: list[str] = []
    n = len(buf)
    i = start + 1
    depth = 1
    while i < n and depth > 0:
        b = buf[i]
        if b == 0x5C:
            i += 1
            if i >= n:
                break
            esc = buf[i]
            if esc == ord("n"):
                out.append("\n")
            elif esc == ord("r"):
                out.append("\r")
            elif esc == ord("t"):
                out.append("\t")
            elif esc == ord("b"):
                out.append("\b")
            elif esc == ord("f"):
                out.append("\f")
            elif esc in (ord("("), ord(")"), 0x5C):
                out.append(chr(esc))
            elif esc in (0x0A, 0x0D):
                if esc == 0x0D and i + 1 < n and buf[i + 1] == 0x0A:
                    i += 1
            elif 0x30 <= esc <= 0x37:
                oct_digits = chr(esc)
                for _ in range(2):
                    if i + 1 < n and 0x30 <= buf[i + 1] <= 0x37:
                        i += 1
                        oct_digits += chr(buf[i])
                    else:
                        break
                out.append(chr(int(oct_digits, 8)))
            else:
                out.append(chr(esc))
            i += 1
        elif b == 0x28:
            depth += 1
            out.append("(")
            i += 1
        elif b == 0x29:
            depth -= 1
            if depth > 0:
                out.append(")")
            i += 1
        else:
            out.append(chr(b))
            i += 1
    return "".join(out), i


def _read_pdf_hex_string(buf: bytes, start: int) -> tuple[str, int]:
    """Parse a PDF <hex> string starting at `start`."""
    end = buf.find(b">", start + 1)
    if end == -1:
        return "", len(buf)
    hex_chars = re.sub(rb"\s+", b"", buf[start + 1 : end]).decode("ascii", errors="ignore")
    if len(hex_chars) % 2 == 1:
        hex_chars += "0"
    try:
        raw = bytes.fromhex(hex_chars)
    except ValueError:
        return "", end + 1
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="ignore"), end + 1
    try:
        return raw.decode("utf-8"), end + 1
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="ignore"), end + 1


def _read_pdf_array(buf: bytes, start: int) -> tuple[list[tuple[str, str | float]], int]:
    """Parse a PDF [...] array containing literal/hex strings and numeric kerning offsets."""
    items: list[tuple[str, str | float]] = []
    n = len(buf)
    i = start + 1
    while i < n:
        b = buf[i]
        if b in _PDF_WHITESPACE:
            i += 1
            continue
        if b == 0x5D:
            return items, i + 1
        if b == 0x28:
            text, i = _read_pdf_literal_string(buf, i)
            items.append(("str", text))
        elif b == 0x3C:
            text, i = _read_pdf_hex_string(buf, i)
            items.append(("str", text))
        else:
            j = i
            while j < n and buf[j] not in _PDF_WHITESPACE and buf[j] not in _PDF_DELIMITERS:
                j += 1
            if j == i:
                i += 1
                continue
            token = buf[i:j]
            i = j
            with contextlib.suppress(ValueError):
                items.append(("num", float(token)))
    return items, n


def _extract_pdf_text_from_stream(stream_bytes: bytes) -> list[str]:
    """Extract normalized lines from PDF BT..ET content operators including Tj and TJ arrays."""
    lines: list[str] = []
    current_parts: list[str] = []
    operands: list[tuple[str, object]] = []
    in_text_obj = False
    last_tm_y: float | None = None

    def flush_line() -> None:
        nonlocal current_parts
        if current_parts:
            joined = "".join(current_parts)
            cleaned = " ".join(joined.split())
            if cleaned:
                lines.append(cleaned)
            current_parts = []

    n = len(stream_bytes)
    i = 0
    while i < n:
        b = stream_bytes[i]
        if b in _PDF_WHITESPACE:
            i += 1
            continue
        if b == 0x25:
            nl = stream_bytes.find(b"\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if b == 0x28:
            text, i = _read_pdf_literal_string(stream_bytes, i)
            if in_text_obj:
                operands.append(("str", text))
            continue
        if b == 0x5B:
            arr_items, i = _read_pdf_array(stream_bytes, i)
            if in_text_obj:
                operands.append(("arr", arr_items))
            continue
        if b == 0x3C:
            if i + 1 < n and stream_bytes[i + 1] == 0x3C:
                close_dict = stream_bytes.find(b">>", i + 2)
                i = n if close_dict == -1 else close_dict + 2
            else:
                text, i = _read_pdf_hex_string(stream_bytes, i)
                if in_text_obj:
                    operands.append(("str", text))
            continue
        if b == 0x2F:
            i += 1
            while (
                i < n
                and stream_bytes[i] not in _PDF_WHITESPACE
                and stream_bytes[i] not in _PDF_DELIMITERS
            ):
                i += 1
            continue
        if b in _PDF_DELIMITERS:
            i += 1
            continue

        j = i
        while (
            j < n
            and stream_bytes[j] not in _PDF_WHITESPACE
            and stream_bytes[j] not in _PDF_DELIMITERS
        ):
            j += 1
        token = stream_bytes[i:j]
        i = j

        if token == b"BT":
            flush_line()
            in_text_obj = True
            last_tm_y = None
            operands.clear()
            continue
        if token == b"ET":
            flush_line()
            in_text_obj = False
            last_tm_y = None
            operands.clear()
            continue
        if not in_text_obj:
            continue

        if token in (b"Tj", b"'", b'"'):
            if token in (b"'", b'"'):
                flush_line()
            for kind, val in reversed(operands):
                if kind == "str" and isinstance(val, str):
                    current_parts.append(val)
                    break
            operands.clear()
        elif token == b"TJ":
            for kind, val in reversed(operands):
                if kind == "arr" and isinstance(val, list):
                    for item_kind, item_val in val:
                        if item_kind == "str" and isinstance(item_val, str):
                            current_parts.append(item_val)
                        elif (
                            item_kind == "num"
                            and isinstance(item_val, float)
                            and item_val <= -120
                            and current_parts
                            and not current_parts[-1].endswith((" ", "\t"))
                        ):
                            current_parts.append(" ")
                    break
            operands.clear()
        elif token == b"T*":
            flush_line()
            operands.clear()
        elif token in (b"Td", b"TD"):
            nums = [float(v) for k, v in operands[-2:] if k == "num" and isinstance(v, float)]
            if len(nums) == 2:
                tx, ty = nums
                if abs(ty) > 0.5:
                    flush_line()
                elif abs(tx) > 0.5 and current_parts and not current_parts[-1].endswith(" "):
                    current_parts.append(" ")
            operands.clear()
        elif token == b"Tm":
            nums = [float(v) for k, v in operands[-6:] if k == "num" and isinstance(v, float)]
            if len(nums) == 6:
                tm_y = nums[5]
                if last_tm_y is not None and abs(tm_y - last_tm_y) > 0.5:
                    flush_line()
                last_tm_y = tm_y
            operands.clear()
        else:
            try:
                operands.append(("num", float(token)))
            except ValueError:
                operands.clear()

    flush_line()
    return lines


def _decompress_pdf_flate_stream(raw_stream: bytes, *, max_bytes: int) -> bytes:
    """Safely decompress a FlateDecode PDF stream enforcing a strict expansion cap."""
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        decompressor = zlib.decompressobj(wbits)
        chunks: list[bytes] = []
        total = 0
        try:
            pos = 0
            chunk_size = 65_536
            while pos < len(raw_stream):
                piece = raw_stream[pos : pos + chunk_size]
                pos += chunk_size
                remaining = max_bytes - total
                if remaining <= 0:
                    raise AppError(
                        code="UPLOAD_REJECTED",
                        message="Rasio ekspansi stream PDF melebihi batas aman.",
                        status_code=422,
                    )
                out = decompressor.decompress(piece, remaining + 1)
                total += len(out)
                if total > max_bytes:
                    raise AppError(
                        code="UPLOAD_REJECTED",
                        message="Rasio ekspansi stream PDF melebihi batas aman.",
                        status_code=422,
                    )
                if out:
                    chunks.append(out)
            tail = decompressor.flush(max(0, max_bytes - total + 1))
            total += len(tail)
            if total > max_bytes:
                raise AppError(
                    code="UPLOAD_REJECTED",
                    message="Rasio ekspansi stream PDF melebihi batas aman.",
                    status_code=422,
                )
            if tail:
                chunks.append(tail)
            return b"".join(chunks)
        except zlib.error:
            if wbits == zlib.MAX_WBITS:
                continue
            raise AppError(
                code="UPLOAD_REJECTED",
                message="Stream FlateDecode pada file PDF rusak atau tidak valid.",
                status_code=422,
            ) from None
    raise AppError(
        code="UPLOAD_REJECTED",
        message="Stream FlateDecode pada file PDF rusak atau tidak valid.",
        status_code=422,
    )


def _validate_pdf_bytes(
    data: bytes,
    *,
    max_file_size_bytes: int,
    max_archive_expansion_ratio: int,
) -> tuple[str, dict[str, object]]:
    """Validate PDF structure, FlateDecode streams, active content, and extract text."""
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File PDF rusak atau tidak memiliki penanda PDF yang valid.",
            status_code=422,
        )
    if _PDF_ENCRYPT_RE.search(data):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File PDF terenkripsi tidak didukung.",
            status_code=422,
        )
    if _PDF_ACTIVE_CONTENT_RE.search(data):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File PDF mengandung JavaScript atau konten aktif yang ditolak.",
            status_code=422,
        )

    max_uncompressed = max_file_size_bytes * max_archive_expansion_ratio
    small_floor = min(_SMALL_DOCX_UNCOMPRESSED_FLOOR_BYTES, max_file_size_bytes)
    effective_cap = min(
        max_uncompressed,
        max(len(data) * max_archive_expansion_ratio, small_floor),
    )

    content_buffers: list[bytes] = []
    remaining_budget = effective_cap
    search_pos = 0
    saw_stream = False

    while True:
        match = re.search(rb"stream(?:\r\n|\n|\r)", data[search_pos:])
        if match is None:
            break
        saw_stream = True
        stream_kw_pos = search_pos + match.start()
        stream_data_start = search_pos + match.end()
        end_pos = data.find(b"endstream", stream_data_start)
        if end_pos == -1:
            raise AppError(
                code="UPLOAD_REJECTED",
                message="Blok stream PDF tidak valid.",
                status_code=422,
            )
        search_pos = end_pos + len(b"endstream")

        raw_stream = data[stream_data_start:end_pos]
        if raw_stream.endswith(b"\r\n"):
            raw_stream = raw_stream[:-2]
        elif raw_stream.endswith((b"\n", b"\r")):
            raw_stream = raw_stream[:-1]

        dict_window = data[max(0, stream_kw_pos - 2048) : stream_kw_pos]
        obj_pos = dict_window.rfind(b"obj")
        dict_start = (
            dict_window.find(b"<<", obj_pos) if obj_pos != -1 else dict_window.rfind(b"<<")
        )
        dict_bytes = dict_window[dict_start:] if dict_start != -1 else dict_window
        dict_lower = dict_bytes.lower()

        if b"/image" in dict_lower or b"/dctdecode" in dict_lower or b"/jpxdecode" in dict_lower:
            continue

        if b"/flatedecode" in dict_lower:
            decoded_stream = _decompress_pdf_flate_stream(raw_stream, max_bytes=remaining_budget)
            remaining_budget -= len(decoded_stream)
            if _PDF_ACTIVE_CONTENT_RE.search(decoded_stream):
                raise AppError(
                    code="UPLOAD_REJECTED",
                    message="File PDF mengandung JavaScript atau konten aktif yang ditolak.",
                    status_code=422,
                )
            content_buffers.append(decoded_stream)
        elif re.search(rb"/filter(?:\s|\[)", dict_lower):
            continue
        else:
            if len(raw_stream) > remaining_budget:
                raise AppError(
                    code="UPLOAD_REJECTED",
                    message="Rasio ekspansi stream PDF melebihi batas aman.",
                    status_code=422,
                )
            remaining_budget -= len(raw_stream)
            if _PDF_ACTIVE_CONTENT_RE.search(raw_stream):
                raise AppError(
                    code="UPLOAD_REJECTED",
                    message="File PDF mengandung JavaScript atau konten aktif yang ditolak.",
                    status_code=422,
                )
            content_buffers.append(raw_stream)

    if not saw_stream:
        content_buffers.append(data)

    page_blocks: list[str] = []
    for buf in content_buffers:
        lines = _extract_pdf_text_from_stream(buf)
        if lines:
            page_blocks.append("\n".join(lines))

    return "\n".join(page_blocks), {
        "pdf_header": data[:8].decode("ascii", errors="replace"),
        "stream_count": len(content_buffers),
        "text_stream_count": len(page_blocks),
    }


def _validate_txt_bytes(data: bytes) -> tuple[str, dict[str, object]]:
    """Validate plain UTF-8 TXT content without NUL bytes."""
    if b"\x00" in data:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File teks mengandung karakter biner yang tidak valid.",
            status_code=422,
        )
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File teks harus menggunakan encoding UTF-8 yang valid.",
            status_code=422,
        ) from exc
    normalized = decoded.strip()
    if not normalized:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File teks tidak boleh kosong.",
            status_code=422,
        )
    return normalized, {"char_count": len(normalized)}


def validate_uploaded_content(
    *,
    filename: str,
    content_type: str,
    data: bytes,
    max_file_size_bytes: int,
    max_archive_expansion_ratio: int,
    max_zip_entries: int,
) -> ValidatedUploadResult:
    """Run complete M5 declaration, magic-byte, bomb, XXE, and content validation."""
    kind, normalized_mime, _ = validate_upload_declaration(
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        max_file_size_bytes=max_file_size_bytes,
    )

    if kind == "docx":
        extracted_text, meta = _validate_docx_bytes(
            data,
            max_file_size_bytes=max_file_size_bytes,
            max_archive_expansion_ratio=max_archive_expansion_ratio,
            max_zip_entries=max_zip_entries,
        )
    elif kind == "pdf":
        extracted_text, meta = _validate_pdf_bytes(
            data,
            max_file_size_bytes=max_file_size_bytes,
            max_archive_expansion_ratio=max_archive_expansion_ratio,
        )
    else:
        extracted_text, meta = _validate_txt_bytes(data)

    return ValidatedUploadResult(
        kind=kind,
        content_type=normalized_mime,
        size_bytes=len(data),
        content_hash=hashlib.sha256(data).hexdigest(),
        extracted_text=extracted_text,
        metadata=meta,
    )

