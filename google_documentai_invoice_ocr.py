from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def _guess_mime_type(file_path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if mime_type:
        return mime_type
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".bmp":
        return "image/bmp"
    if suffix == ".tif" or suffix == ".tiff":
        return "image/tiff"
    return "application/octet-stream"


def _load_credentials_json(
    credentials_file: str = "",
    credentials_json: str = "",
) -> str:
    inline_json = str(credentials_json or "").strip()
    if inline_json:
        return inline_json

    path_value = str(credentials_file or "").strip()
    if not path_value:
        return ""

    return Path(path_value).read_text(encoding="utf-8")


def _text_anchor_text(text_anchor: Any, full_text: str) -> str:
    if not text_anchor:
        return ""

    parts: List[str] = []
    for segment in list(getattr(text_anchor, "text_segments", []) or []):
        start_index = int(getattr(segment, "start_index", 0) or 0)
        end_index = int(getattr(segment, "end_index", 0) or 0)
        if end_index > start_index:
            parts.append(full_text[start_index:end_index])
    return "".join(parts)


def _layout_bbox(layout: Any, page: Any) -> Optional[Dict[str, Any]]:
    if not layout:
        return None

    bounding_poly = getattr(layout, "bounding_poly", None)
    if not bounding_poly:
        return None

    vertices = list(getattr(bounding_poly, "normalized_vertices", []) or [])
    if not vertices:
        vertices = list(getattr(bounding_poly, "vertices", []) or [])

    points: List[Dict[str, float]] = []
    for vertex in vertices:
        x = getattr(vertex, "x", None)
        y = getattr(vertex, "y", None)
        if x is None or y is None:
            continue
        points.append({"x": float(x), "y": float(y)})

    if not points:
        return None

    xs = [point["x"] for point in points]
    ys = [point["y"] for point in points]
    page_width = float(getattr(getattr(page, "dimension", None), "width", 0) or 0)
    page_height = float(getattr(getattr(page, "dimension", None), "height", 0) or 0)

    is_normalized = max(xs) <= 1.0 and max(ys) <= 1.0
    return {
        "points": points,
        "left": min(xs),
        "top": min(ys),
        "right": max(xs),
        "bottom": max(ys),
        "page_width": page_width,
        "page_height": page_height,
        "normalized": is_normalized,
    }


def _entity_summary(entity: Any, full_text: str) -> Dict[str, Any]:
    page_refs: List[int] = []
    for ref in list(getattr(entity, "page_anchor", None) and getattr(entity.page_anchor, "page_refs", None) or []):
        page_no = getattr(ref, "page", None)
        if page_no is not None:
            page_refs.append(int(page_no) + 1)

    normalized_value = getattr(entity, "normalized_value", None)
    value = getattr(entity, "mention_text", None) or _text_anchor_text(getattr(entity, "text_anchor", None), full_text)
    data: Dict[str, Any] = {
        "type": str(getattr(entity, "type_", "") or ""),
        "mention_text": str(getattr(entity, "mention_text", "") or ""),
        "value": str(value or ""),
        "confidence": float(getattr(entity, "confidence", 0.0) or 0.0),
        "page_refs": page_refs,
    }
    if normalized_value is not None:
        data["normalized_value"] = str(normalized_value)
    return data


def run_document_ai_ocr(
    file_path: Path,
    *,
    project_id: str,
    location: str,
    processor_id: str,
    credentials_json: str = "",
    include_entities: bool = False,
) -> Dict[str, Any]:
    from google.cloud import documentai  # type: ignore
    from google.oauth2 import service_account  # type: ignore

    mime_type = _guess_mime_type(file_path)
    file_bytes = file_path.read_bytes()
    client_options = {"api_endpoint": f"{location}-documentai.googleapis.com"}

    if credentials_json:
        info = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(info)
        client = documentai.DocumentProcessorServiceClient(
            credentials=credentials,
            client_options=client_options,
        )
    else:
        client = documentai.DocumentProcessorServiceClient(client_options=client_options)

    processor_name = client.processor_path(project_id, location, processor_id)
    raw_document = documentai.RawDocument(content=file_bytes, mime_type=mime_type)
    request = documentai.ProcessRequest(name=processor_name, raw_document=raw_document)
    result = client.process_document(request=request)
    document = result.document
    full_text = str(getattr(document, "text", "") or "")

    pages_out: List[Dict[str, Any]] = []
    line_text_parts: List[str] = []
    ocr_lines: List[Dict[str, Any]] = []

    for page_index, page in enumerate(list(getattr(document, "pages", []) or []), start=1):
        page_lines: List[str] = []
        for line in list(getattr(page, "lines", []) or []):
            layout = getattr(line, "layout", None)
            text = _text_anchor_text(getattr(layout, "text_anchor", None), full_text).strip()
            if not text:
                continue
            page_lines.append(text)
            line_text_parts.append(text)
            ocr_lines.append(
                {
                    "page": page_index,
                    "text": text,
                    "bbox": _layout_bbox(layout, page),
                }
            )

        pages_out.append(
            {
                "page": page_index,
                "width": float(getattr(getattr(page, "dimension", None), "width", 0) or 0),
                "height": float(getattr(getattr(page, "dimension", None), "height", 0) or 0),
                "line_count": len(page_lines),
                "line_text": "\n".join(page_lines).strip(),
            }
        )

    payload: Dict[str, Any] = {
        "source_file": str(file_path),
        "mime_type": mime_type,
        "processor": {
            "project_id": project_id,
            "location": location,
            "processor_id": processor_id,
        },
        "page_count": len(pages_out),
        "raw_text": full_text,
        "line_text": "\n".join(line_text_parts).strip(),
        "pages": pages_out,
        "ocr_lines": ocr_lines,
    }

    if include_entities:
        payload["entities"] = [
            _entity_summary(entity, full_text)
            for entity in list(getattr(document, "entities", []) or [])
        ]

    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract OCR data from an invoice using Google Cloud Document AI.",
    )
    parser.add_argument("invoice", help="Path to the invoice file (PDF or image).")
    parser.add_argument(
        "--project-id",
        default=os.getenv("GDOC_PROJECT_ID", ""),
        help="Google Cloud project id. Defaults to GDOC_PROJECT_ID.",
    )
    parser.add_argument(
        "--location",
        default=os.getenv("GDOC_LOCATION", "us"),
        help="Document AI processor location. Defaults to GDOC_LOCATION or 'us'.",
    )
    parser.add_argument(
        "--processor-id",
        default=os.getenv("GDOC_OCR_PROCESSOR_ID", ""),
        help="Document AI OCR processor id. Defaults to GDOC_OCR_PROCESSOR_ID.",
    )
    parser.add_argument(
        "--credentials-file",
        default=os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
        help="Path to a service-account JSON file. Defaults to GOOGLE_APPLICATION_CREDENTIALS.",
    )
    parser.add_argument(
        "--credentials-json",
        default=os.getenv("GDOC_CREDENTIALS_JSON", ""),
        help="Raw service-account JSON string. Optional alternative to --credentials-file.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional output JSON path. If omitted, JSON is printed to stdout.",
    )
    parser.add_argument(
        "--include-entities",
        action="store_true",
        help="Include Document AI entities if the processor returns them.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    invoice_path = Path(args.invoice).expanduser().resolve()
    if not invoice_path.is_file():
        parser.error(f"Invoice file not found: {invoice_path}")

    project_id = str(args.project_id or "").strip()
    location = str(args.location or "").strip()
    processor_id = str(args.processor_id or "").strip()
    if not project_id or not location or not processor_id:
        parser.error("Missing Document AI settings. Provide --project-id, --location, and --processor-id.")

    credentials_json = _load_credentials_json(
        credentials_file=args.credentials_file,
        credentials_json=args.credentials_json,
    )

    result = run_document_ai_ocr(
        invoice_path,
        project_id=project_id,
        location=location,
        processor_id=processor_id,
        credentials_json=credentials_json,
        include_entities=bool(args.include_entities),
    )

    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.write_text(rendered, encoding="utf-8")
        print(f"Wrote OCR JSON to {output_path}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
