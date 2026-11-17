"""Running the command line tools backups depend on (pg_basebackup, zstd, age) in constant memory."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

CHUNK = 1024 * 1024


class ToolError(RuntimeError):
    """A tool failed. The message says which and what it printed (never a password)."""


def path_of(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ToolError(f"{name} is not installed on this machine")
    return found


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def fsync_dir(path: Path) -> None:
    import os

    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def pipe_to_zstd(
    source_cmd: list[str],
    dest: Path,
    env: dict[str, str] | None = None,
    level: int = 3,
    timeout: float | None = None,
) -> int:
    """`source_cmd | zstd -<level> > dest`, one megabyte at a time. Returns the compressed size.

    The two processes are joined by a pipe the operating system manages, so memory stays flat however
    large the source is. `timeout` (seconds) kills both tools if they take longer. The caller writes to a
    temporary name and renames it, so dest is complete or absent.
    """
    with dest.open("wb") as out:
        src = subprocess.Popen(source_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)  # noqa: S603
        try:
            zst = subprocess.Popen(  # noqa: S603
                [path_of("zstd"), f"-{level}", "-q", "-c"],
                stdin=src.stdout,
                stdout=out,
                stderr=subprocess.PIPE,
            )
            if src.stdout is None:
                raise ToolError("the source tool gave no output")
            src.stdout.close()  # so zstd sees the end of input when the source exits
            zerr = zst.communicate(timeout=timeout)[1]
            serr = src.stderr.read() if src.stderr else b""
            src.wait(timeout=30)
        except subprocess.TimeoutExpired:
            src.kill()
            zst.kill()
            raise ToolError(
                f"{Path(source_cmd[0]).name} did not finish within {timeout:.0f} seconds"
            ) from None
        except BaseException:
            src.kill()
            raise
        out.flush()
        import os

        os.fsync(out.fileno())
    if src.returncode != 0:
        raise ToolError(
            f"{Path(source_cmd[0]).name} failed ({src.returncode}): {serr.decode(errors='replace')[-800:]}"
        )
    if zst.returncode != 0:
        raise ToolError(f"zstd failed ({zst.returncode}): {zerr.decode(errors='replace')[-800:]}")
    return dest.stat().st_size


def age_encrypt(src: Path, dest: Path, recipients_file: Path) -> int:
    """Encrypt a file to the public key(s) in recipients_file. Streams; the private key is never here."""
    with src.open("rb") as inp, dest.open("wb") as out:
        proc = subprocess.run(  # noqa: S603
            [path_of("age"), "-R", str(recipients_file)],
            stdin=inp,
            stdout=out,
            stderr=subprocess.PIPE,
            check=False,
        )
        out.flush()
        import os

        os.fsync(out.fileno())
    if proc.returncode != 0:
        dest.unlink(missing_ok=True)
        raise ToolError(f"age failed ({proc.returncode}): {proc.stderr.decode(errors='replace')[-800:]}")
    return dest.stat().st_size
