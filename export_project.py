import os
from pathlib import Path

# Specify directories or files to ignore
EXCLUDE_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules", "logs", "data"}
EXCLUDE_FILES = {"export_project.py", "export_all_code.txt", "package-lock.json"}
ALLOWED_EXTENSIONS = {".py", ".env", ".json", ".yml", ".yaml", ".html", ".css", ".js", ".md"}

def export_code():
    root_dir = Path(__file__).parent
    output_file = root_dir / "export_all_code.txt"

    print(f"📦 Exporting project files from: {root_dir}")

    with open(output_file, "w", encoding="utf-8") as outfile:
        outfile.write("=" * 80 + "\n")
        outfile.write(" CODENEXUS AI - FULL PROJECT SOURCE CODE EXPORT\n")
        outfile.write("=" * 80 + "\n\n")

        file_count = 0
        for path in sorted(root_dir.rglob("*")):
            # Skip directories or files in exclusion lists
            if any(part in EXCLUDE_DIRS for part in path.parts):
                continue
            if path.is_file() and (path.name in EXCLUDE_FILES or path.suffix not in ALLOWED_EXTENSIONS):
                continue
            if path.is_file():
                relative_path = path.relative_to(root_dir)
                print(f"📄 Adding: {relative_path}")

                outfile.write(f"\n{'=' * 80}\n")
                outfile.write(f"FILE: {relative_path}\n")
                outfile.write(f"{'=' * 80}\n\n")

                try:
                    with open(path, "r", encoding="utf-8") as infile:
                        outfile.write(infile.read())
                except Exception as e:
                    outfile.write(f"[Error reading file: {e}]\n")

                outfile.write("\n\n")
                file_count += 1

    print(f"\n✅ Successfully exported {file_count} files into: {output_file.name}")

if __name__ == "__main__":
    export_code()
