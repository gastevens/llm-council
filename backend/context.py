"""Context folders: let the council discuss the files in a folder.

    ctx = build_context("~/Documents/Proposal")
    prompt = stage1_prompt(question, ctx)

Two modes:

* ``inline`` (default when it fits): text is extracted from every readable file
  (plain text, code, Markdown, JSON, CSV, …; PDFs via ``pdftotext`` or
  ``pypdf``; ``.docx`` via the stdlib) and pasted into the prompt with a header
  per file, so every member sees exactly the same material.
* ``folder`` (fallback when the extracted text is over budget, or forced): each
  member runs *inside* the folder with read-only file tools and opens what it
  needs (see ``providers``). The prompt carries a manifest of the files.

Environment:

    COUNCIL_CONTEXT_MAX_CHARS       inline budget in characters (default 400000)
    COUNCIL_CONTEXT_MAX_FILE_CHARS  per-file cap before a file is truncated (default 150000)
    COUNCIL_CONTEXT_MAX_FILES       most files pasted inline (default 400)
    COUNCIL_CONTEXT_REVIEW_CHARS    stage 2/3 get the full text up to this size,
                                    else just the manifest (default 150000)
    COUNCIL_CONTEXT_MODE            auto | inline | folder (default auto)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

MODES = ("auto", "inline", "folder")

SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "venv", "env", "site-packages", "dist", "build", "target",
    "bower_components", "vendor", "Pods", "DerivedData", "coverage", "htmlcov",
})

TEXT_EXTS = frozenset("""
txt md markdown mdx rst org adoc tex bib log
py pyi ipynb js mjs cjs jsx ts tsx vue svelte astro rb php go rs java kt kts scala swift m mm
c h cc cpp cxx hpp hh cs fs vb lua pl pm r jl dart ex exs erl hrl clj cljs elm hs ml nim zig
sh bash zsh fish ps1 bat cmd
json jsonl ndjson yaml yml toml ini cfg conf env.example properties xml plist
csv tsv sql graphql gql proto
html htm css scss sass less
gitignore dockerignore editorconfig dockerfile makefile mk cmake gradle
srt vtt eml ics vcf
""".split())

BINARY_EXTS = frozenset("""
png jpg jpeg gif webp bmp tiff tif heic ico icns psd ai sketch fig
mp3 wav aac flac ogg m4a mp4 mov avi mkv webm
zip gz tgz bz2 xz 7z rar tar dmg iso pkg
exe dll so dylib a o class jar pyc pyo wasm bin
ttf otf woff woff2 eot
sqlite sqlite3 db
xlsx xls pptx ppt key numbers pages doc odt ods odp
""".split())

LOCKFILES = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock",
                       "Cargo.lock", "Gemfile.lock", "composer.lock", ".DS_Store"})

SCAN_LIMIT = 5000           # stop walking after this many files
PDF_TIMEOUT = 60


class ContextError(ValueError):
    """A context path that can't be used (missing, unreadable, not allowed)."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def max_chars_default() -> int:
    return _env_int("COUNCIL_CONTEXT_MAX_CHARS", 400_000)


def review_chars_default() -> int:
    return _env_int("COUNCIL_CONTEXT_REVIEW_CHARS", 150_000)


def mode_default() -> str:
    m = (os.getenv("COUNCIL_CONTEXT_MODE") or "auto").strip().lower()
    return m if m in MODES else "auto"


@dataclass
class ContextFile:
    path: str                    # relative to the context root, "/" separated
    chars: int                   # characters extracted (or an estimate when not read)
    kind: str = "text"           # text | pdf | docx
    text: Optional[str] = None   # None when not read (over budget)
    truncated: bool = False


@dataclass
class FolderContext:
    root: Path
    files: List[ContextFile] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (relpath, reason)
    max_chars: int = 400_000
    mode: str = "inline"         # resolved: inline | folder
    requested_mode: str = "auto"
    reason: str = ""             # why folder mode was picked
    scan_truncated: bool = False
    display: Optional[str] = None  # label override (e.g. "3 Slack attachments")

    @property
    def label(self) -> str:
        return self.display or self.root.name or str(self.root)

    @property
    def total_chars(self) -> int:
        return sum(f.chars for f in self.files)

    @property
    def over_budget(self) -> bool:
        return (self.total_chars > self.max_chars or self.scan_truncated
                or any(f.truncated or f.text is None for f in self.files)
                or len(self.files) > _env_int("COUNCIL_CONTEXT_MAX_FILES", 400))

    def summary(self) -> str:
        n = len(self.files)
        size = _human(self.total_chars)
        mode = "inline" if self.mode == "inline" else "folder access"
        return f"{self.label} · {n} file{'s' if n != 1 else ''} · {size} chars · {mode}"

    def file_list(self, limit: int = 50) -> List[str]:
        names = [f.path for f in self.files]
        return names[:limit] + ([f"… and {len(names) - limit} more"] if len(names) > limit else [])


def _human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


# ---------------------------------------------------------------------------
# Walking
# ---------------------------------------------------------------------------

def _git_files(root: Path) -> Optional[List[str]]:
    """Tracked + untracked-but-not-ignored files under root, if root is in a git work tree."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        inside = subprocess.run([git, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True, timeout=10)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        out = subprocess.run([git, "-C", str(root), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard", "--", "."],
                             capture_output=True, timeout=30)
        if out.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    return sorted({p for p in out.stdout.decode("utf-8", "replace").split("\0") if p})


def _walk(root: Path) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                             and not d.endswith((".app", ".egg-info")))
        rel_dir = os.path.relpath(dirpath, root)
        for name in sorted(filenames):
            yield name if rel_dir == "." else f"{rel_dir}/{name}".replace(os.sep, "/")


def _skip_reason(rel: str) -> Optional[str]:
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts):
        return "hidden"
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return "dependency/build folder"
    if parts[-1] in LOCKFILES:
        return "lockfile"
    return None


def _priority(rel: str) -> Tuple[int, int, str]:
    name = rel.rsplit("/", 1)[-1].lower()
    first = 0 if name.startswith(("readme", "index", "overview", "summary")) else 1
    return (rel.count("/"), first, rel.lower())


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _ext(path: Path) -> str:
    name = path.name.lower()
    if name in ("dockerfile", "makefile", "readme", "license", "changelog"):
        return name
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _read_text(path: Path, cap: int) -> Optional[Tuple[str, bool]]:
    """Decode a file as text (None if it looks binary). Returns (text, truncated)."""
    with open(path, "rb") as f:
        raw = f.read(cap * 4 + 4)
    if b"\0" in raw[:8192]:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            # tolerate a multibyte char cut off by the read cap
            text = raw[:-3].decode("utf-8") if len(raw) > cap * 4 else None
        except UnicodeDecodeError:
            text = None
        if text is None:
            sample = raw[:8192].decode("latin-1")
            printable = sum(c.isprintable() or c in "\r\n\t" for c in sample)
            if not sample or printable / len(sample) < 0.95:
                return None
            text = raw.decode("latin-1")
    truncated = len(text) > cap or os.path.getsize(path) > len(raw)
    return text[:cap], truncated


def _read_pdf(path: Path, cap: int) -> Tuple[Optional[str], str]:
    exe = shutil.which("pdftotext") or next(
        (p for p in ("/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext") if os.path.exists(p)), None)
    if exe:
        try:
            out = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(path), "-"],
                                 capture_output=True, timeout=PDF_TIMEOUT)
            if out.returncode == 0:
                return out.stdout.decode("utf-8", "replace"), ""
            return None, "PDF could not be read"
        except (OSError, subprocess.SubprocessError):
            return None, "PDF could not be read"
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return None, "PDF (install poppler's pdftotext to include PDFs)"
    try:
        reader = PdfReader(str(path))
        chunks, size = [], 0
        for page in reader.pages:
            t = page.extract_text() or ""
            chunks.append(t)
            size += len(t)
            if size > cap:
                break
        return "\n\n".join(chunks), ""
    except Exception:
        return None, "PDF could not be read"


def _read_docx(path: Path) -> Tuple[Optional[str], str]:
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return None, "Word file could not be read"
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html
    return html.unescape(text), ""


def _extract(path: Path, cap: int) -> Tuple[Optional[str], str, bool, str]:
    """Returns (text, kind, truncated, skip_reason)."""
    ext = _ext(path)
    if ext == "pdf":
        text, why = _read_pdf(path, cap)
        if text is None:
            return None, "pdf", False, why
        text = text.strip()
        if not text:
            return None, "pdf", False, "PDF has no extractable text (scanned?)"
        return text[:cap], "pdf", len(text) > cap, ""
    if ext == "docx":
        text, why = _read_docx(path)
        if text is None:
            return None, "docx", False, why
        return text[:cap], "docx", len(text) > cap, ""
    if ext in BINARY_EXTS:
        return None, "", False, f"binary (.{ext})"
    res = _read_text(path, cap)
    if res is None:
        return None, "", False, "binary"
    text, truncated = res
    return text, "text", truncated, ""


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def resolve_path(raw: str, base: Optional[str] = None) -> Path:
    """Expand ~ and $VARS, resolve relative to `base` (default cwd), and check it exists."""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        raise ContextError("No folder given.")
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = Path(base or os.getcwd()) / p
    try:
        p = p.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        raise ContextError(f"{raw} doesn't exist.")
    if not os.access(p, os.R_OK):
        raise ContextError(f"{p} isn't readable.")
    return p


def build_context(path, max_chars: Optional[int] = None, mode: Optional[str] = None,
                  base: Optional[str] = None, display: Optional[str] = None) -> FolderContext:
    """Scan `path` (a folder, or a single file) and decide inline vs folder mode."""
    root = path if isinstance(path, Path) and path.is_absolute() else resolve_path(str(path), base)
    budget = max_chars if max_chars is not None else max_chars_default()
    mode = (mode or mode_default()).lower()
    if mode not in MODES:
        raise ContextError(f"Unknown context mode '{mode}'. Use auto, inline or folder.")
    per_file = min(_env_int("COUNCIL_CONTEXT_MAX_FILE_CHARS", 150_000), budget)

    single_file = root.is_file()
    if single_file:
        rels = [root.name]
        base_dir = root.parent
    else:
        base_dir = root
        listed = _git_files(root)
        rels = listed if listed is not None else list(_walk(root))

    ctx = FolderContext(root=root, max_chars=budget, requested_mode=mode, display=display)
    rels = sorted(rels, key=_priority)
    if len(rels) > SCAN_LIMIT:
        ctx.scan_truncated = True
        rels = rels[:SCAN_LIMIT]

    total = 0
    for rel in rels:
        why = None if single_file else _skip_reason(rel)
        if why:
            ctx.skipped.append((rel, why))
            continue
        full = base_dir / rel
        try:
            if full.is_symlink() and not single_file:
                target = full.resolve()
                if base_dir not in target.parents:
                    ctx.skipped.append((rel, "symlink outside the folder"))
                    continue
            if not full.is_file():
                continue
            size = full.stat().st_size
        except OSError:
            ctx.skipped.append((rel, "unreadable"))
            continue
        if size == 0:
            ctx.skipped.append((rel, "empty"))
            continue
        if total > budget:
            # Already over budget: list the file without reading it (folder mode reads it).
            ext = _ext(full)
            if ext in BINARY_EXTS:
                ctx.skipped.append((rel, f"binary (.{ext})"))
            else:
                ctx.files.append(ContextFile(rel, size, kind="pdf" if ext == "pdf" else "text"))
            continue
        try:
            text, kind, truncated, why = _extract(full, per_file)
        except OSError:
            ctx.skipped.append((rel, "unreadable"))
            continue
        if text is None:
            ctx.skipped.append((rel, why or "unsupported"))
            continue
        ctx.files.append(ContextFile(rel, len(text), kind=kind, text=text, truncated=truncated))
        total += len(text)

    if not ctx.files:
        what = "file" if single_file else "folder"
        raise ContextError(f"No readable text files in that {what} ({len(ctx.skipped)} skipped).")

    if mode == "folder":
        ctx.mode, ctx.reason = "folder", "folder access requested"
    elif mode == "inline":
        ctx.mode = "inline"
    elif ctx.over_budget:
        ctx.mode = "folder"
        ctx.reason = (f"too big to paste ({_human(ctx.total_chars)} chars, "
                      f"{len(ctx.files)} files; budget {_human(budget)})")
    else:
        ctx.mode = "inline"
    return ctx


# ---------------------------------------------------------------------------
# Prompt text
# ---------------------------------------------------------------------------

CITE = ("Ground your answer in these files and cite the file paths you rely on, relative to "
        "the shared folder (e.g. `notes/plan.md`), quoting short passages where it helps. "
        "Say so if the files don't cover something.")


def inline_block(ctx: FolderContext, max_chars: Optional[int] = None) -> str:
    """The files pasted with per-file headers, cut at the budget, plus what was left out."""
    budget = max_chars if max_chars is not None else ctx.max_chars
    out = [f'<context folder="{ctx.label}">']
    used, omitted = 0, []
    for f in ctx.files:
        if f.text is None or used + len(f.text) > budget:
            room = budget - used
            if f.text is not None and room > 2000 and not omitted:
                out.append(f"===== FILE: {f.path} (first {room:,} of {f.chars:,} chars) =====")
                out.append(f.text[:room])
                out.append(f"===== END FILE: {f.path} (truncated) =====")
                used = budget
            omitted.append(f.path)
            continue
        note = " (truncated)" if f.truncated else ""
        out.append(f"===== FILE: {f.path}{note} =====")
        out.append(f.text.rstrip())
        out.append(f"===== END FILE: {f.path} =====")
        used += len(f.text)
    if omitted:
        out.append(f"Not included (over the size budget): {', '.join(omitted[:200])}"
                   + (f" … and {len(omitted) - 200} more" if len(omitted) > 200 else ""))
    if ctx.skipped:
        out.append(_skipped_line(ctx))
    out.append("</context>")
    return "\n".join(out)


def _skipped_line(ctx: FolderContext, limit: int = 40) -> str:
    items = [f"{p} ({why})" for p, why in ctx.skipped[:limit]]
    more = f" … and {len(ctx.skipped) - limit} more" if len(ctx.skipped) > limit else ""
    return "Skipped (not text): " + "; ".join(items) + more


def manifest(ctx: FolderContext, limit: int = 300) -> str:
    lines = [f"Files in `{ctx.label}` ({len(ctx.files)} readable, {_human(ctx.total_chars)} chars):"]
    for f in ctx.files[:limit]:
        tag = "" if f.kind == "text" else f" [{f.kind}]"
        lines.append(f"- {f.path} ({f.chars:,} chars){tag}")
    if len(ctx.files) > limit:
        lines.append(f"- … and {len(ctx.files) - limit} more")
    if ctx.scan_truncated:
        lines.append(f"(Listing stopped after {SCAN_LIMIT} files.)")
    if ctx.skipped:
        lines.append(_skipped_line(ctx))
    return "\n".join(lines)


def stage1_prompt(question: str, ctx: Optional[FolderContext]) -> str:
    """The Stage 1 prompt: the question, with the context folder in front of it."""
    if ctx is None:
        return question
    if ctx.mode == "folder":
        return (
            f"The user shared a folder of files for this question. It is your current working "
            f"directory; open, list and search the files you need with your read-only tools "
            f"(don't modify anything). {CITE}\n\n{manifest(ctx)}\n\n---\n\n{question}"
        )
    return (
        f"The user shared the following files for this question. {CITE}\n\n"
        f"{inline_block(ctx)}\n\n---\n\n{question}"
    )


def review_prefix(ctx: Optional[FolderContext], role: str = "reviewer") -> str:
    """Context note for Stage 2 (reviewers) and Stage 3 (chair).

    Inline contexts up to COUNCIL_CONTEXT_REVIEW_CHARS are repeated in full so
    reviewers can check citations; bigger ones get the manifest only. In folder
    mode reviewers run inside the folder too and may open files to verify.
    """
    if ctx is None:
        return ""
    who = "Reviewers" if role == "reviewer" else "You"
    if ctx.mode == "folder":
        return (f"The question is about a folder of files, which is your current working directory. "
                f"{who} may open files with read-only tools to check the answers' claims and citations.\n\n"
                f"{manifest(ctx)}\n\n---\n\n")
    if ctx.total_chars <= review_chars_default():
        return (f"The question is about these files; use them to check the answers' claims and "
                f"citations.\n\n{inline_block(ctx)}\n\n---\n\n")
    return (f"The question is about a set of files that the council members were given in full "
            f"(too large to repeat here). Judge citations against this list:\n\n"
            f"{manifest(ctx)}\n\n---\n\n")


def describe(ctx: Optional[FolderContext]) -> str:
    """One-paragraph human description (TUI notices, Slack, ask.py)."""
    if ctx is None:
        return "No context folder."
    extra = ""
    if ctx.mode == "folder":
        extra = (f" Members open the files themselves because it's {ctx.reason}."
                 if ctx.reason.startswith("too big") else " Folder access was requested.")
    skipped = f" {len(ctx.skipped)} skipped." if ctx.skipped else ""
    return f"Context: {ctx.root} — {ctx.summary()}.{skipped}{extra}"
