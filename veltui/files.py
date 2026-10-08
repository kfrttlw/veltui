"""Attaching text files to a message, and completing their paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

# a giant paste chokes duck.ai's composer; 100 KB of code is already a lot
MAX_BYTES = 100_000

LANG_BY_EXT = {
    ".py": "python", ".pyw": "python", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx", ".json": "json",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "fish", ".ps1": "powershell",
    ".rb": "ruby", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".swift": "swift", ".sql": "sql",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".conf": "ini",
    ".cfg": "ini", ".md": "markdown", ".xml": "xml", ".lua": "lua", ".qml": "qml",
    ".r": "r", ".pl": "perl", ".dart": "dart", ".scala": "scala", ".vue": "vue",
    ".nix": "nix", ".zig": "zig",
}


@dataclass(frozen=True)
class Attachment:
    name: str
    content: str
    lang: str
    lines: int

    def block(self) -> str:
        """The file as a fenced code block, the way it's sent."""
        fence = _fence_for(self.content)
        return f"{self.name}:\n{fence}{self.lang}\n{self.content}\n{fence}"


def clean_path(s: str) -> str:
    """A pasted path without quotes or a file:// wrapper (file managers paste those)."""
    s = s.strip().strip('"').strip("'").strip()
    if s.startswith("file://"):
        p = unquote(urlparse(s).path)
        if len(p) >= 3 and p[0] == "/" and p[2] == ":":      # /C:/x → C:/x
            p = p[1:]
        s = p
    return s


def as_path(s: str) -> Path | None:
    """`s` as an existing path on disk, or None."""
    try:
        p = Path(clean_path(s)).expanduser()
        return p if clean_path(s) and p.exists() else None
    except (OSError, ValueError):
        return None


def split_path_arg(arg: str) -> tuple[str, str]:
    """`/file` argument → (path, question).

    A quoted path ends at its closing quote. Unquoted, the path is the longest
    leading run of words that exists on disk, so `notes.txt sum it up` and
    `my notes.txt` (a file with a space) both work.
    """
    arg = arg.strip()
    if arg[:1] in ('"', "'"):
        end = arg.find(arg[0], 1)
        if end != -1:
            return arg[1:end], arg[end + 1:].strip()
        return arg[1:].strip(), ""
    words = arg.split(" ")
    for i in range(len(words), 0, -1):
        if as_path(" ".join(words[:i])):
            return " ".join(words[:i]), " ".join(words[i:]).strip()
    return arg, ""


def read(path_arg: str) -> Attachment:
    """Read a text file. Raises OSError (missing, a folder, unreadable) or
    ValueError (too big, binary) with a message fit to show."""
    p = Path(clean_path(path_arg)).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {path_arg}")
    if p.is_dir():
        raise IsADirectoryError(f"that's a folder: {path_arg}")
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise ValueError(f"{p.name} is {size // 1024} KB — files are capped at "
                         f"{MAX_BYTES // 1000} KB")
    data = p.read_bytes()
    try:
        if b"\x00" in data:
            raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL byte")
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{p.name} looks binary — only text and code can be sent") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return Attachment(p.name, text, LANG_BY_EXT.get(p.suffix.lower(), ""),
                      text.count("\n") + 1 if text else 0)


def _fence_for(content: str) -> str:
    """A fence longer than any backtick run in the file, so it can't break out."""
    longest = run = 0
    for ch in content:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def human_size(p: Path) -> str:
    try:
        n = float(p.stat().st_size)
    except OSError:
        return ""
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}T"


def completions(arg: str, limit: int = 40) -> list[tuple[str, str, str]]:
    """(new argument, label, detail) for the path being typed after `/file `.

    Folders end with "/" so Tab can step into them; files get a trailing space
    so whatever comes next is the question. Nothing once a question has begun.
    """
    if arg.startswith(('"', "'")):
        if arg[0] in arg[1:]:
            return []
        typed, quote = arg[1:], arg[0]
    else:
        path, question = split_path_arg(arg)
        if question or (arg.endswith(" ") and as_path(path) and as_path(path).is_file()):
            return []
        typed, quote = arg, ""
    if typed.endswith(("/", "\\")) or not typed:
        base = Path(clean_path(typed) or ".").expanduser()
        prefix = ""
    else:
        p = Path(clean_path(typed)).expanduser()
        base, prefix = p.parent, p.name
    head = typed[: len(typed) - len(prefix)]
    try:
        entries = sorted(base.iterdir(), key=lambda e: (not _is_dir(e), e.name.lower()))
    except OSError:
        return []
    out = []
    low = prefix.lower()
    for e in entries:
        if e.name.startswith(".") and not prefix.startswith("."):
            continue
        if not e.name.lower().startswith(low):
            continue
        d = _is_dir(e)
        fill = f"{quote}{head}{e.name}" + ("/" if d else (quote + " "))
        out.append((fill, e.name + ("/" if d else ""), "dir" if d else human_size(e)))
        if len(out) >= limit:
            break
    return out


def _is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False
