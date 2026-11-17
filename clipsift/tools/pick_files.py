"""Native file dialog, run in its own process (Tk needs the main thread). Prints a JSON list."""
import argparse
import json
import sys

KINDS = {
    "video": [("Video files", "*.mp4 *.mkv *.mov *.flv *.ts *.webm *.m4v"), ("All files", "*.*")],
    "csv": [("CSV files", "*.csv"), ("All files", "*.*")],
    "image": [("Images", "*.png *.jpg *.jpeg"), ("All files", "*.*")],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="video", choices=list(KINDS))
    ap.add_argument("--single", action="store_true")
    args = ap.parse_args()
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    if args.single or args.kind == "image":
        p = filedialog.askopenfilename(filetypes=KINDS[args.kind], title="Choose a file")
        paths = [p] if p else []
    else:
        paths = list(filedialog.askopenfilenames(filetypes=KINDS[args.kind], title="Choose files"))
    root.destroy()
    sys.stdout.write(json.dumps(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
