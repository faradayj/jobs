import sys
import subprocess
import argparse
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Compile LaTeX resumes to PDFs.")
    parser.add_argument("file", nargs="?", help="Specific .tex file to compile (e.g., resume_cat1_agentic_llm.tex)")
    parser.add_argument("--all", action="store_true", help="Compile all .tex files in data/resumes")
    
    args = parser.parse_args()
    
    project_root = Path(__file__).parent.parent
    resumes_dir = project_root / "data" / "resumes"
    tex_dir = resumes_dir / "tex"
    output_dir = resumes_dir / "generated_pdfs"
    
    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)
    
    tex_files = []
    if args.all:
        tex_files = list(tex_dir.glob("*.tex"))
    elif args.file:
        file_path = tex_dir / args.file
        if file_path.exists():
            tex_files.append(file_path)
        elif (tex_dir / f"{args.file}.tex").exists():
            tex_files.append(tex_dir / f"{args.file}.tex")
        else:
            print(f"Error: Could not find {args.file} in {tex_dir}")
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)
        
    for tex_file in tex_files:
        print(f"Compiling {tex_file.name}...")
        cmd = [
            "pdflatex",
            f"-output-directory={output_dir.absolute()}",
            str(tex_file.absolute())
        ]
        
        try:
            subprocess.run(cmd, check=True, cwd=project_root)
            print(f"Successfully compiled {tex_file.name} to {output_dir}")
        except subprocess.CalledProcessError as e:
            print(f"Error compiling {tex_file.name}: {e}")
            sys.exit(1)

if __name__ == "__main__":
    main()
