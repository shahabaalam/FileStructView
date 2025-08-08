import os
import sys
import zipfile
import tarfile
from collections import Counter
from typing import Dict, Any, Optional, List

# Optional extras (used only if installed)
try:
    import py7zr  # type: ignore
    _HAS_PY7ZR = True
except Exception:
    _HAS_PY7ZR = False

try:
    import rarfile  # type: ignore
    _HAS_RARFILE = True
except Exception:
    _HAS_RARFILE = False


# -----------------------
# Core summarizer + print
# -----------------------

def _ext_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return ext if ext else "<noext>"

def _normalize_path(p: str) -> str:
    p = p.strip().strip('"').strip("'")
    p = os.path.expanduser(os.path.expandvars(p))
    return os.path.abspath(p)

def _insert_path_into_tree(root: Dict[str, Any], parts: List[str], ext: str) -> None:
    cur = root
    for p in parts[:-1]:
        nxt = None
        for d in cur["dirs"]:
            if d["name"] == p:
                nxt = d
                break
        if nxt is None:
            nxt = {"name": p, "dirs": [], "ext_counts": Counter()}
            cur["dirs"].append(nxt)
        cur = nxt
    cur["ext_counts"][ext] += 1

def _accumulate_counts(n: Dict[str, Any]) -> None:
    for d in n["dirs"]:
        _accumulate_counts(d)
        n["ext_counts"].update(d["ext_counts"])

def _build_fs_tree(path: str) -> Dict[str, Any]:
    # Single file → single node
    if os.path.isfile(path):
        node = {"name": os.path.basename(path) or path, "dirs": [], "ext_counts": Counter()}
        node["ext_counts"][_ext_of(path)] += 1
        return node

    node = {"name": os.path.basename(path) or path, "dirs": [], "ext_counts": Counter()}
    try:
        with os.scandir(path) as it:
            subdirs: List[str] = []
            direct_exts = Counter()
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        direct_exts[_ext_of(entry.name)] += 1
                except PermissionError:
                    pass
            for sub in sorted(subdirs, key=lambda p: os.path.basename(p).lower()):
                child = _build_fs_tree(sub)
                node["dirs"].append(child)
            cumulative = Counter(direct_exts)
            for child in node["dirs"]:
                cumulative.update(child.get("ext_counts", Counter()))
            node["ext_counts"] = cumulative
    except PermissionError:
        node["perm_denied"] = True
    return node

def _build_zip_tree(zip_path: str) -> Dict[str, Any]:
    root = {"name": os.path.basename(zip_path), "dirs": [], "ext_counts": Counter()}
    with zipfile.ZipFile(zip_path, 'r') as z:
        for name in z.namelist():
            if name.endswith('/'):
                continue
            parts = [p for p in name.split('/') if p]
            if not parts:
                continue
            ext = _ext_of(parts[-1])
            _insert_path_into_tree(root, parts, ext)
    _accumulate_counts(root)
    return root

def _build_tar_tree(tar_path: str) -> Dict[str, Any]:
    root = {"name": os.path.basename(tar_path), "dirs": [], "ext_counts": Counter()}
    with tarfile.open(tar_path, "r:*") as tf:
        for m in tf.getmembers():
            if m.isdir():
                continue
            parts = [p for p in m.name.split('/') if p]
            if not parts:
                continue
            ext = _ext_of(parts[-1])
            _insert_path_into_tree(root, parts, ext)
    _accumulate_counts(root)
    return root

def _build_7z_tree(sevenz_path: str) -> Dict[str, Any]:
    if not _HAS_PY7ZR:
        raise RuntimeError("py7zr is not installed. Install with: pip install py7zr")
    root = {"name": os.path.basename(sevenz_path), "dirs": [], "ext_counts": Counter()}
    with py7zr.SevenZipFile(sevenz_path, mode='r') as z:
        for name in z.getnames():
            parts = [p for p in name.split('/') if p]
            if not parts:
                continue
            ext = _ext_of(parts[-1])
            _insert_path_into_tree(root, parts, ext)
    _accumulate_counts(root)
    return root

def _build_rar_tree(rar_path: str) -> Dict[str, Any]:
    if not _HAS_RARFILE:
        raise RuntimeError("rarfile is not installed. Install with: pip install rarfile")
    root = {"name": os.path.basename(rar_path), "dirs": [], "ext_counts": Counter()}
    with rarfile.RarFile(rar_path) as rf:
        for info in rf.infolist():
            if info.isdir():
                continue
            parts = [p for p in info.filename.split('/') if p]
            if not parts:
                continue
            ext = _ext_of(parts[-1])
            _insert_path_into_tree(root, parts, ext)
    _accumulate_counts(root)
    return root

def build_tree_summary(path: str) -> Dict[str, Any]:
    p = _normalize_path(path)
    if os.path.isdir(p):
        return _build_fs_tree(p)
    if os.path.isfile(p):
        if zipfile.is_zipfile(p):
            return _build_zip_tree(p)
        if tarfile.is_tarfile(p):
            return _build_tar_tree(p)
        if p.lower().endswith(".7z"):
            if _HAS_PY7ZR:
                return _build_7z_tree(p)
            node = {"name": os.path.basename(p), "dirs": [], "ext_counts": Counter(), "note": "Install py7zr to inspect inside this 7z archive."}
            node["ext_counts"][_ext_of(p)] += 1
            return node
        if p.lower().endswith(".rar"):
            if _HAS_RARFILE:
                return _build_rar_tree(p)
            node = {"name": os.path.basename(p), "dirs": [], "ext_counts": Counter(), "note": "Install rarfile to inspect inside this RAR archive."}
            node["ext_counts"][_ext_of(p)] += 1
            return node
        # Regular file fallback
        return _build_fs_tree(p)
    raise ValueError(f"Path not found: {p}")

def format_tree_summary(node: Dict[str, Any], top_k_exts: int = 5, max_depth: Optional[int] = None) -> str:
    """Return the tree summary as a string."""
    lines: List[str] = []
    def _walk(n: Dict[str, Any], indent: str = "", last: bool = True, depth: int = 0):
        if max_depth is not None and depth > max_depth:
            return
        branch = "└── " if last else "├── "
        total = sum(n.get("ext_counts", {}).values())
        top = n.get("ext_counts", Counter()).most_common(top_k_exts)
        parts = [f"{k.lstrip('.')}: {v}" for k, v in top]
        if len(n.get("ext_counts", {})) > top_k_exts:
            parts.append("…")
        if n.get("perm_denied"):
            label = f"[{n['name']}] (permission denied)"
        else:
            label = f"[{n['name']}] (total: {total}" + (f" | {', '.join(parts)})" if total else ")")
        lines.append(indent + branch + label)
        if max_depth is not None and depth == max_depth:
            return
        kids = n.get("dirs", [])
        for i, d in enumerate(kids):
            _walk(d, indent + ("    " if last else "│   "), i == len(kids) - 1, depth + 1)
    _walk(node, "", True, 0)
    return "\n".join(lines)


# -----------------------
# Visualizations
# -----------------------

def plot_stacked_bar_by_ext(root: Dict[str, Any], top_k_exts: int = 6):
    import matplotlib.pyplot as plt

    children = root.get("dirs", [])
    if not children:
        print("No subfolders to plot.")
        return

    all_exts = Counter()
    for d in children:
        all_exts.update(d["ext_counts"])
    top_exts = [e for e, _ in all_exts.most_common(top_k_exts)]
    labels = [d["name"] for d in children]
    series = {ext: [] for ext in top_exts}
    other = []
    for d in children:
        counts = d["ext_counts"]
        for ext in top_exts:
            series[ext].append(counts.get(ext, 0))
        other.append(sum(v for k, v in counts.items() if k not in top_exts))

    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 0.9), 5))
    bottom = [0] * len(labels)
    for ext in top_exts:
        ax.bar(labels, series[ext], bottom=bottom, label=ext.lstrip('.'))
        bottom = [b + v for b, v in zip(bottom, series[ext])]
    if any(other):
        ax.bar(labels, other, bottom=bottom, label="other")

    ax.set_title(f"File-type counts per subfolder under '{root['name']}'")
    ax.set_xlabel("Subfolder")
    ax.set_ylabel("Count")
    ax.tick_params(axis='x', labelrotation=45)
    # Align tick labels properly
    plt.setp(ax.get_xticklabels(), ha='right')

    ax.legend()
    plt.tight_layout()
    plt.show()

def plot_treemap(root: Dict[str, Any]):
    """
    Interactive treemap with plotly.graph_objects.
    Each rectangle size = total files in that folder.
    pip install plotly
    """
    try:
        import plotly.graph_objects as go
    except Exception as e:
        raise RuntimeError("Plotly not installed. Run: pip install plotly") from e

    labels: List[str] = []
    ids: List[str] = []
    parents: List[str] = []
    values: List[int] = []

    def tot(n): return int(sum(n.get("ext_counts", {}).values()))

    def walk(n: Dict[str, Any], parent_id: str, path: List[str]):
        node_id = "/".join(path + [n["name"]]) if path else n["name"]
        labels.append(n["name"])
        ids.append(node_id)
        parents.append(parent_id)
        values.append(tot(n))
        for d in n.get("dirs", []):
            walk(d, node_id, path + [n["name"]])

    # Root has empty parent
    walk(root, "", [])

    fig = go.Figure(go.Treemap(
        labels=labels,
        ids=ids,
        parents=parents,
        values=values,
        branchvalues="total",
        hovertemplate="<b>%{label}</b><br>Total files: %{value}<extra></extra>"
    ))
    fig.update_layout(
        margin=dict(l=8, r=8, t=30, b=8),
        title=f"Treemap – {root['name']}"
    )
    fig.show()


# -----------------------
# Minimal Tkinter GUI
# -----------------------

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root_win = tk.Tk()
    root_win.title("Folder/Archive Summarizer")

    # --- Controls frame ---
    frm = ttk.Frame(root_win, padding=10)
    frm.grid(row=0, column=0, sticky="nsew")
    root_win.rowconfigure(0, weight=1)
    root_win.columnconfigure(0, weight=1)

    path_var = tk.StringVar()
    depth_var = tk.StringVar(value="")
    topk_var = tk.StringVar(value="5")

    ttk.Label(frm, text="Target (folder / archive / file):").grid(row=0, column=0, sticky="w")
    path_entry = ttk.Entry(frm, textvariable=path_var, width=70)
    path_entry.grid(row=1, column=0, columnspan=4, sticky="we", pady=(0,6))

    def pick_folder():
        p = filedialog.askdirectory(title="Select Folder")
        if p:
            path_var.set(p)

    def pick_file():
        p = filedialog.askopenfilename(
            title="Select Archive or File",
            filetypes=[
                ("All supported", "*.zip;*.tar;*.tgz;*.tar.gz;*.tbz2;*.tar.bz2;*.txz;*.tar.xz;*.7z;*.rar;*.*"),
                ("ZIP", "*.zip"),
                ("TAR", "*.tar *.tgz *.tar.gz *.tbz2 *.tar.bz2 *.txz *.tar.xz"),
                ("7z", "*.7z"),
                ("RAR", "*.rar"),
                ("All files", "*.*"),
            ]
        )
        if p:
            path_var.set(p)

    def copy_path():
        path = path_var.get().strip()
        if not path:
            messagebox.showinfo("Copy Path", "No path to copy. Select a folder or file first.")
            return
        root_win.clipboard_clear()
        root_win.clipboard_append(path)
        root_win.update()
        messagebox.showinfo("Copy Path", "Path copied to clipboard.")

    ttk.Button(frm, text="Select Folder…", command=pick_folder).grid(row=1, column=4, padx=(6,0))
    ttk.Button(frm, text="Select File…", command=pick_file).grid(row=1, column=5, padx=(6,0))
    ttk.Button(frm, text="Copy Path", command=copy_path).grid(row=1, column=6, padx=(6,0))

    ttk.Label(frm, text="Max depth (blank = unlimited):").grid(row=2, column=0, sticky="w", pady=(6,0))
    depth_entry = ttk.Entry(frm, textvariable=depth_var, width=10)
    depth_entry.grid(row=2, column=1, sticky="w", pady=(6,0))

    ttk.Label(frm, text="Top-K extensions per line:").grid(row=2, column=2, sticky="e", pady=(6,0))
    topk_entry = ttk.Entry(frm, textvariable=topk_var, width=6)
    topk_entry.grid(row=2, column=3, sticky="w", pady=(6,0))

    # --- Output text ---
    txt = tk.Text(frm, wrap="none", height=28)
    txt.grid(row=3, column=0, columnspan=7, sticky="nsew", pady=(8,0))
    frm.rowconfigure(3, weight=1)
    frm.columnconfigure(0, weight=1)
    xscroll = tk.Scrollbar(frm, orient="horizontal", command=txt.xview)
    xscroll.grid(row=4, column=0, columnspan=7, sticky="we")
    yscroll = tk.Scrollbar(frm, orient="vertical", command=txt.yview)
    yscroll.grid(row=3, column=7, sticky="ns")
    txt.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)

    # --- Actions ---
    def analyze():
        path = path_var.get().strip()
        if not path:
            messagebox.showwarning("Pick something", "Please select a folder, archive, or file.")
            return

        try:
            md = None if depth_var.get().strip() == "" else int(depth_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid depth", "Max depth must be an integer or blank.")
            return

        try:
            tk_ = int(topk_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid Top-K", "Top-K must be an integer.")
            return

        try:
            tree = build_tree_summary(path)
            text = format_tree_summary(tree, top_k_exts=tk_, max_depth=md)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return

        txt.delete("1.0", "end")
        txt.insert("1.0", text)

        if note := tree.get("note"):
            txt.insert("end", f"\n\nNote: {note}")

        root_win._last_tree = tree
        root_win._last_topk = tk_
        root_win._last_depth = md

    def show_bar():
        tree = getattr(root_win, "_last_tree", None)
        if tree is None:
            messagebox.showinfo("Nothing to plot", "Run Analyze first.")
            return
        try:
            plot_stacked_bar_by_ext(tree, top_k_exts=getattr(root_win, "_last_topk", 6))
        except Exception as e:
            messagebox.showerror("Plot error", str(e))

    def show_treemap():
        tree = getattr(root_win, "_last_tree", None)
        if tree is None:
            messagebox.showinfo("Nothing to plot", "Run Analyze first.")
            return
        try:
            plot_treemap(tree)
        except Exception as e:
            messagebox.showerror("Treemap error", str(e))

    ttk.Button(frm, text="Analyze", command=analyze).grid(row=5, column=0, sticky="w", pady=(8,0))
    ttk.Button(frm, text="Stacked Bar (types)", command=show_bar).grid(row=5, column=1, sticky="w", padx=(6,0), pady=(8,0))
    ttk.Button(frm, text="Treemap (Plotly)", command=show_treemap).grid(row=5, column=2, sticky="w", padx=(6,0), pady=(8,0))

    root_win.minsize(1000, 650)
    root_win.mainloop()


# -----------------------
# Entry point
# -----------------------

if __name__ == "__main__":
    # CLI mode: python folder.py <path> [max_depth] [top_k]
    if len(sys.argv) > 1:
        target = sys.argv[1]
        max_depth = int(sys.argv[2]) if len(sys.argv) >= 3 and sys.argv[2].isdigit() else None
        top_k = int(sys.argv[3]) if len(sys.argv) >= 4 and sys.argv[3].isdigit() else 5
        tree = build_tree_summary(target)
        print(format_tree_summary(tree, top_k_exts=top_k, max_depth=max_depth))
        # Uncomment to pop plots from CLI:
        # plot_stacked_bar_by_ext(tree, top_k_exts=6)
        # plot_treemap(tree)  # requires plotly
    else:
        run_gui()
