"""Create local configuration from fictional examples, preserving existing files."""

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def initialize(root: Path) -> list[Path]:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    created = []
    for example, name in (("library.example.json", "library.json"), (".env.example", ".env")):
        contents = (root / "examples" / example).read_bytes()
        destination = data / name
        try:
            with destination.open("xb") as handle:
                handle.write(contents)
        except FileExistsError:
            continue
        created.append(destination)
    return created


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    created = initialize(ROOT)
    for path in created:
        print(f"Created {path.relative_to(ROOT)}")
    if not created:
        print("Local configuration already exists; preserved existing files.")
    print("Edit data/library.json and data/.env; add your resume PDF before applying.")


if __name__ == "__main__":
    main()
