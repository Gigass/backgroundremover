#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}

RESAMPLING = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS


@dataclass
class CompressStats:
    processed: int = 0
    compressed: int = 0
    copied_original: int = 0
    errors: int = 0
    bytes_saved: int = 0
    format_changed: int = 0
    resized: int = 0


def has_alpha(image: Image.Image) -> bool:
    if "A" in image.getbands():
        return True
    return "transparency" in image.info


def choose_output(image: Image.Image, source_path: Path) -> tuple[str, str]:
    ext = source_path.suffix.lower()
    alpha = has_alpha(image)

    if ext in {".jpg", ".jpeg"}:
        return "JPEG", ".jpg"
    if ext == ".png":
        return "PNG", ".png"
    if ext == ".webp":
        return "WEBP", ".webp"
    if ext in {".tif", ".tiff"}:
        return "TIFF", ".tiff"
    if ext in {".bmp", ".heic", ".heif"}:
        return ("PNG", ".png") if alpha else ("JPEG", ".jpg")
    return ("PNG", ".png") if alpha else ("JPEG", ".jpg")


def apply_aggressive_preset(args: argparse.Namespace) -> None:
    if not args.aggressive:
        return

    args.allow_format_conversion = True
    args.jpg_quality = min(args.jpg_quality, 82)
    args.webp_quality = min(args.webp_quality, 80)
    args.jpg_subsampling = max(args.jpg_subsampling, 2)

    if args.png_palette_colors == 0:
        args.png_palette_colors = 256
    if args.max_edge == 0:
        args.max_edge = 2560
    if args.min_saving_kb < 2:
        args.min_saving_kb = 2


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.jpg_quality <= 100:
        raise SystemExit("--jpg-quality must be between 1 and 100")
    if not 1 <= args.webp_quality <= 100:
        raise SystemExit("--webp-quality must be between 1 and 100")
    if not 0 <= args.png_compress_level <= 9:
        raise SystemExit("--png-compress-level must be between 0 and 9")
    if not 0 <= args.jpg_subsampling <= 2:
        raise SystemExit("--jpg-subsampling must be 0, 1, or 2")
    if not 0 <= args.png_palette_colors <= 256:
        raise SystemExit("--png-palette-colors must be between 0 and 256")
    if args.max_edge < 0:
        raise SystemExit("--max-edge must be >= 0")
    if args.min_saving_kb < 0:
        raise SystemExit("--min-saving-kb must be >= 0")


def build_save_kwargs(image: Image.Image, output_format: str, args: argparse.Namespace) -> dict:
    kwargs: dict = {}

    exif = image.info.get("exif")
    if exif:
        kwargs["exif"] = exif

    icc_profile = image.info.get("icc_profile")
    if icc_profile:
        kwargs["icc_profile"] = icc_profile

    if output_format == "JPEG":
        kwargs.update(
            {
                "quality": args.jpg_quality,
                "optimize": True,
                "progressive": True,
                "subsampling": args.jpg_subsampling,
            }
        )
    elif output_format == "PNG":
        kwargs.update(
            {
                "optimize": True,
                "compress_level": args.png_compress_level,
            }
        )
    elif output_format == "WEBP":
        kwargs.update(
            {
                "quality": args.webp_quality,
                "method": 6,
                "alpha_quality": args.webp_quality,
            }
        )
    elif output_format == "TIFF":
        kwargs.update({"compression": "tiff_lzw"})

    return kwargs


def prepare_image_for_save(
    image: Image.Image,
    output_format: str,
    args: argparse.Namespace,
) -> Image.Image:
    if output_format == "JPEG":
        if has_alpha(image):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        if image.mode not in {"RGB", "L"}:
            return image.convert("RGB")
    elif output_format == "WEBP":
        if has_alpha(image):
            if image.mode != "RGBA":
                return image.convert("RGBA")
            return image
        if image.mode not in {"RGB", "L"}:
            return image.convert("RGB")
    elif output_format == "PNG":
        if args.png_palette_colors > 0:
            colors = max(2, min(256, args.png_palette_colors))
            quantize_method = (
                Image.Quantize.FASTOCTREE if has_alpha(image) else Image.Quantize.MEDIANCUT
            )
            base = image.convert("RGBA") if has_alpha(image) else image.convert("RGB")
            return base.quantize(colors=colors, method=quantize_method)
    return image


def maybe_resize(image: Image.Image, max_edge: int) -> tuple[Image.Image, bool]:
    if max_edge <= 0:
        return image, False

    width, height = image.size
    longest_edge = max(width, height)
    if longest_edge <= max_edge:
        return image, False

    scale = max_edge / float(longest_edge)
    new_size = (
        max(1, int(round(width * scale))),
        max(1, int(round(height * scale))),
    )
    resized = image.resize(new_size, RESAMPLING)
    return resized, True


def dedupe_candidates(candidates: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def choose_candidate_outputs(
    image: Image.Image,
    source_path: Path,
    args: argparse.Namespace,
) -> list[tuple[str, str]]:
    base = choose_output(image=image, source_path=source_path)
    candidates: list[tuple[str, str]] = [base]

    if args.allow_format_conversion:
        if has_alpha(image):
            candidates.extend([("WEBP", ".webp"), ("PNG", ".png")])
        else:
            candidates.extend([("WEBP", ".webp"), ("JPEG", ".jpg")])

    return dedupe_candidates(candidates)


def destination_path_for(
    source_path: Path,
    input_dir: Path,
    output_dir: Path,
    output_ext: str,
) -> Path:
    relative = source_path.relative_to(input_dir)
    source_ext = relative.suffix.lower()

    if source_ext == output_ext:
        return output_dir / relative.with_suffix(output_ext)

    source_tag = source_ext[1:] if source_ext.startswith(".") else "img"
    output_name = f"{relative.stem}_from_{source_tag}{output_ext}"
    return output_dir / relative.with_name(output_name)


def write_candidate(
    image: Image.Image,
    source_image: Image.Image,
    output_format: str,
    output_ext: str,
    args: argparse.Namespace,
) -> Path | None:
    fd, tmp_name = tempfile.mkstemp(suffix=output_ext)
    os.close(fd)
    tmp_path = Path(tmp_name)

    save_kwargs = build_save_kwargs(source_image, output_format, args)
    prepared = prepare_image_for_save(image, output_format, args)

    try:
        try:
            prepared.save(tmp_path, format=output_format, **save_kwargs)
        except TypeError:
            fallback_kwargs = {
                key: value
                for key, value in save_kwargs.items()
                if key not in {"exif", "icc_profile"}
            }
            prepared.save(tmp_path, format=output_format, **fallback_kwargs)
        return tmp_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        return None


def iter_images(input_dir: Path, output_dir: Path) -> list[Path]:
    images: list[Path] = []
    output_dir_resolved = output_dir.resolve()

    for path in input_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        resolved = path.resolve()
        if resolved == output_dir_resolved or output_dir_resolved in resolved.parents:
            continue

        images.append(path)

    return images


def compress_one(
    source_path: Path,
    input_dir: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> tuple[str, int, bool, bool, Path]:
    with Image.open(source_path) as image:
        resized_image, resized = maybe_resize(image, args.max_edge)
        candidate_outputs = choose_candidate_outputs(resized_image, source_path, args)

        original_size = source_path.stat().st_size
        min_saving_bytes = int(args.min_saving_kb * 1024)

        best_tmp: Path | None = None
        best_size = original_size
        best_ext = source_path.suffix.lower()

        for output_format, output_ext in candidate_outputs:
            tmp_path = write_candidate(
                image=resized_image,
                source_image=image,
                output_format=output_format,
                output_ext=output_ext,
                args=args,
            )
            if tmp_path is None:
                continue

            candidate_size = tmp_path.stat().st_size
            if candidate_size < best_size:
                if best_tmp is not None:
                    best_tmp.unlink(missing_ok=True)
                best_tmp = tmp_path
                best_size = candidate_size
                best_ext = output_ext
            else:
                tmp_path.unlink(missing_ok=True)

        if best_tmp is not None and (original_size - best_size) >= min_saving_bytes:
            dest_path = destination_path_for(
                source_path=source_path,
                input_dir=input_dir,
                output_dir=output_dir,
                output_ext=best_ext,
            )
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if dest_path.exists():
                dest_path.unlink()
            shutil.move(str(best_tmp), str(dest_path))

            format_changed = source_path.suffix.lower() != best_ext
            return "compressed", original_size - best_size, format_changed, resized, dest_path

        if best_tmp is not None:
            best_tmp.unlink(missing_ok=True)

        dest_path = output_dir / source_path.relative_to(input_dir)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if dest_path.exists():
            dest_path.unlink()
        shutil.copy2(source_path, dest_path)
        return "copied", 0, False, False, dest_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compress images under ~/Downloads while preserving visual quality."
    )
    parser.add_argument(
        "--input-dir",
        default="/Users/gigass/Downloads",
        help="Folder to scan for images.",
    )
    parser.add_argument(
        "--output-dir",
        default="/Users/gigass/Downloads/compressed_images",
        help="Folder where compressed copies are written.",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="Stronger compression (more size reduction, small quality trade-off).",
    )
    parser.add_argument(
        "--no-format-conversion",
        dest="allow_format_conversion",
        action="store_false",
        help="Do not change file format (for example PNG->WEBP).",
    )
    parser.set_defaults(allow_format_conversion=True)
    parser.add_argument(
        "--jpg-quality",
        type=int,
        default=88,
        help="JPEG output quality (1-100, default: 88).",
    )
    parser.add_argument(
        "--jpg-subsampling",
        type=int,
        default=1,
        help="JPEG chroma subsampling: 0(best quality)-2(best compression), default: 1.",
    )
    parser.add_argument(
        "--webp-quality",
        type=int,
        default=86,
        help="WEBP output quality (1-100, default: 86).",
    )
    parser.add_argument(
        "--png-compress-level",
        type=int,
        default=9,
        help="PNG compression level (0-9, default: 9).",
    )
    parser.add_argument(
        "--png-palette-colors",
        type=int,
        default=0,
        help="For PNG output, quantize colors to 2-256 (0 disables).",
    )
    parser.add_argument(
        "--max-edge",
        type=int,
        default=0,
        help="Resize images so longer side <= this value (0 keeps original size).",
    )
    parser.add_argument(
        "--min-saving-kb",
        type=float,
        default=1.0,
        help="Only keep compressed result if it saves at least this many KB.",
    )

    args = parser.parse_args()
    apply_aggressive_preset(args)
    validate_args(args)
    return args


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not input_dir.exists():
        raise SystemExit(f"Input directory not found: {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    images = iter_images(input_dir=input_dir, output_dir=output_dir)

    if not images:
        print(f"No images found in {input_dir}")
        return

    stats = CompressStats()
    total = len(images)
    print(f"Found {total} images. Compressing to: {output_dir}")
    if args.aggressive:
        print("Mode: aggressive")
    elif args.allow_format_conversion:
        print("Mode: smart (allows format conversion)")
    else:
        print("Mode: keep original format")

    for index, image_path in enumerate(images, start=1):
        stats.processed += 1
        try:
            result, saved_bytes, format_changed, resized, dest_path = compress_one(
                source_path=image_path,
                input_dir=input_dir,
                output_dir=output_dir,
                args=args,
            )

            if result == "compressed":
                stats.compressed += 1
                stats.bytes_saved += saved_bytes
                if format_changed:
                    stats.format_changed += 1
                if resized:
                    stats.resized += 1

                tags: list[str] = [f"saved {saved_bytes / 1024:.1f} KB"]
                if format_changed:
                    tags.append("format changed")
                if resized:
                    tags.append("resized")
                message = ", ".join(tags)
            else:
                stats.copied_original += 1
                message = "already optimal, copied original"

            print(f"[{index}/{total}] {image_path.name} -> {message} ({dest_path.name})")
        except Exception as exc:
            stats.errors += 1
            print(f"[{index}/{total}] {image_path.name} -> ERROR: {exc}")

    print("\nDone.")
    print(f"Processed: {stats.processed}")
    print(f"Compressed: {stats.compressed}")
    print(f"Copied original: {stats.copied_original}")
    print(f"Errors: {stats.errors}")
    print(f"Format changed: {stats.format_changed}")
    print(f"Resized: {stats.resized}")
    print(f"Total saved: {stats.bytes_saved / (1024 * 1024):.2f} MB")
    print(f"Output folder: {output_dir}")


if __name__ == "__main__":
    main()
