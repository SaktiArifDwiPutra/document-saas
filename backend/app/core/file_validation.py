"""Strict upload validation and archive/XML safety checks (Milestone M5)."""

import hashlib
import io
import re
import zipfile
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


def _validate_pdf_bytes(data: bytes) -> tuple[str, dict[str, object]]:
    """Validate PDF magic header, EOF marker, and reject embedded JS/active actions."""
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File PDF rusak atau tidak memiliki penanda PDF yang valid.",
            status_code=422,
        )
    if _PDF_ACTIVE_CONTENT_RE.search(data):
        raise AppError(
            code="UPLOAD_REJECTED",
            message="File PDF mengandung JavaScript atau konten aktif yang ditolak.",
            status_code=422,
        )
    return "", {"pdf_header": data[:8].decode("ascii", errors="replace")}


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
        extracted_text, meta = _validate_pdf_bytes(data)
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

