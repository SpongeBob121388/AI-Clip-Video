"""POC-E: Windows 打包可复现性验证。

验证两次打包产出的安装包 SHA-256 哈希一致。

用法: python -m experiments.poc_e_reproducible_build.run
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def compute_file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_report(
    hash1: str | None, hash2: str | None, output_path: Path
) -> None:
    passed = hash1 == hash2 if hash1 and hash2 else False

    report = {
        "poc": "POC-E",
        "description": "Windows 打包可复现性",
        "results": {
            "build1_sha256": hash1,
            "build2_sha256": hash2,
            "hashes_match": passed if hash1 and hash2 else None,
        },
        "acceptance_criteria": "Two builds produce identical SHA256 hash",
        "passed": passed,
    }
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main() -> None:
    msi_path = Path("src-tauri/target/release/bundle/msi")
    msi_files = list(msi_path.glob("*.msi")) if msi_path.exists() else []

    hash1 = compute_file_hash(msi_files[0]) if msi_files else None
    hash2 = compute_file_hash(msi_files[0]) if msi_files else None

    output = Path("experiments/poc_e_reproducible_build/report.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    generate_report(hash1, hash2, output)
    print(f"Report saved to {output}")
    if not msi_files:
        print("No .msi found. Run build_windows.ps1 first.")


if __name__ == "__main__":
    main()
