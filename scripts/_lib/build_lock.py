"""Cross-process serialization for knowledge-base readers and builders."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Mapping, Optional


class BuildLockUnavailable(RuntimeError):
    """Another process/thread owns this knowledge base's build transaction."""


class _ReadOnlyLockInfrastructureUnavailable(BuildLockUnavailable):
    """A first reader cannot initialize external locking in its sandbox."""


INSTALL_GENERATION_MARKER = ".openmobius-install-generation.json"
READ_ONLY_BUNDLE_MARKER = ".openmobius-readonly-bundle.json"
READ_ONLY_BUNDLE_FORMAT = "openmobius-readonly-bundle"
READ_ONLY_BUNDLE_FORMAT_VERSION = 1
READ_ONLY_BUNDLE_FILES = (
    "knowledge_v2.compact.json",
    "schools.json",
    "term_aliases.json",
)
_READ_ONLY_BUNDLE_MARKER_MAX_BYTES = 4096
_READ_ONLY_BUNDLE_CORPUS_MAX_BYTES = 8_000_000


_ACTIVE_BUILD_LOCKS: dict[str, dict] = {}
_BORROWABLE_READ_SESSIONS: dict[tuple[int, int, str], dict] = {}
_BUILD_LOCK_REGISTRY_GUARD = threading.Lock()


def _is_read_only_policy_error(exc: OSError) -> bool:
    """Return whether a host policy specifically denied a write-like syscall."""
    return exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS} or getattr(
        exc,
        "winerror",
        None,
    ) == 5


def _uses_windows_locking() -> bool:
    """Isolate the platform branch so missing-lock behavior is testable."""
    return os.name == "nt"


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in read-only bundle marker: {key}")
        result[key] = value
    return result


def _path_entry_present(path: Path) -> bool:
    """Check one directory entry without hiding permission or I/O errors."""
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise BuildLockUnavailable(
            f"cannot inspect read-only bundle marker: {path}: {exc}"
        ) from exc
    return True


def encode_read_only_bundle_marker(file_payloads: Mapping[str, bytes]) -> bytes:
    """Bind every knowledge input shipped by a constrained read-only host."""
    if set(file_payloads) != set(READ_ONLY_BUNDLE_FILES):
        raise ValueError("read-only bundle inputs do not match the runtime file set")
    files: dict[str, dict[str, object]] = {}
    for name in READ_ONLY_BUNDLE_FILES:
        payload = file_payloads[name]
        if (
            not isinstance(payload, bytes)
            or not payload
            or len(payload) > _READ_ONLY_BUNDLE_CORPUS_MAX_BYTES
        ):
            raise ValueError(f"read-only bundle input has an invalid size: {name}")
        files[name] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return (
        json.dumps(
            {
                "files": files,
                "format": READ_ONLY_BUNDLE_FORMAT,
                "format_version": READ_ONLY_BUNDLE_FORMAT_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _read_regular_file(path: Path, *, max_bytes: int) -> bytes:
    """Read one bounded, non-symlink regular file through a stable descriptor."""
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size <= 0
        or before.st_size > max_bytes
    ):
        raise ValueError(f"read-only bundle input is not a safe file: {path}")
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or (details.st_dev, details.st_ino) != (before.st_dev, before.st_ino)
            or details.st_size <= 0
            or details.st_size > max_bytes
        ):
            raise ValueError(f"read-only bundle input is not a safe file: {path}")
        chunks: list[bytes] = []
        remaining = details.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError(f"read-only bundle input was truncated: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"read-only bundle input changed while reading: {path}")
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_read_only_bundle(kb_dir: Path) -> bool:
    """Validate an explicit immutable-bundle marker and its compact corpus."""
    kb_dir = Path(kb_dir)
    marker = kb_dir / READ_ONLY_BUNDLE_MARKER
    if not _path_entry_present(marker):
        return False
    try:
        if kb_dir.is_symlink() or not kb_dir.is_dir():
            raise ValueError(f"read-only bundle directory is unsafe: {kb_dir}")
        marker_payload = _read_regular_file(
            marker,
            max_bytes=_READ_ONLY_BUNDLE_MARKER_MAX_BYTES,
        )
        decoded = json.loads(marker_payload, object_pairs_hook=_strict_json_object)
        if (
            not isinstance(decoded, dict)
            or set(decoded)
            != {"files", "format", "format_version"}
            or decoded.get("format") != READ_ONLY_BUNDLE_FORMAT
            or isinstance(decoded.get("format_version"), bool)
            or not isinstance(decoded.get("format_version"), int)
            or decoded.get("format_version") != READ_ONLY_BUNDLE_FORMAT_VERSION
            or not isinstance(decoded.get("files"), dict)
            or set(decoded["files"]) != set(READ_ONLY_BUNDLE_FILES)
        ):
            raise ValueError("read-only bundle marker has an unsupported format")
        for name in READ_ONLY_BUNDLE_FILES:
            expected = decoded["files"][name]
            if (
                not isinstance(expected, dict)
                or set(expected) != {"bytes", "sha256"}
                or isinstance(expected.get("bytes"), bool)
                or not isinstance(expected.get("bytes"), int)
                or not 0 < expected["bytes"] <= _READ_ONLY_BUNDLE_CORPUS_MAX_BYTES
                or not isinstance(expected.get("sha256"), str)
                or len(expected["sha256"]) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in expected["sha256"]
                )
            ):
                raise ValueError(f"read-only bundle file identity is invalid: {name}")
            payload = _read_regular_file(
                kb_dir / name,
                max_bytes=_READ_ONLY_BUNDLE_CORPUS_MAX_BYTES,
            )
            if (
                len(payload) != expected["bytes"]
                or hashlib.sha256(payload).hexdigest() != expected["sha256"]
            ):
                raise ValueError(f"read-only bundle file hash mismatch: {name}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BuildLockUnavailable(
            f"invalid read-only knowledge bundle: {kb_dir}: {exc}"
        ) from exc
    return True


def _build_lock_key(kb_dir: Path) -> str:
    canonical = str(Path(kb_dir).resolve())
    if sys.platform == "darwin":
        # Default APFS/HFS volumes are case-insensitive and expose canonically
        # equivalent Unicode spellings. Over-serializing on a case-sensitive
        # Darwin volume is safer than allowing two writers for one default FS.
        canonical = unicodedata.normalize("NFC", canonical).casefold()
    else:
        canonical = os.path.normcase(canonical)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def current_thread_holds_kb_lock(kb_dir: Path, *, mode: str) -> bool:
    """Return whether this thread already owns the requested generation lease."""
    key = _build_lock_key(kb_dir)
    with _BUILD_LOCK_REGISTRY_GUARD:
        active = _ACTIVE_BUILD_LOCKS.get(key)
        return bool(
            active
            and active["pid"] == os.getpid()
            and active["mode"] == mode
            and threading.get_ident() in active["owners"]
        )


def current_thread_has_borrowable_read_session(kb_dir: Path) -> bool:
    """Return whether the CLI has explicitly lent its outer read lease."""
    key = _build_lock_key(kb_dir)
    process_id = os.getpid()
    thread_id = threading.get_ident()
    token = (process_id, thread_id, key)
    with _BUILD_LOCK_REGISTRY_GUARD:
        active = _ACTIVE_BUILD_LOCKS.get(key)
        session = _BORROWABLE_READ_SESSIONS.get(token)
        return bool(
            session
            and session["depth"] > 0
            and active
            and active["pid"] == process_id
            and active["mode"] == "read"
            and thread_id in active["owners"]
        )


def register_borrowed_read_cleanup(
    kb_dir: Path,
    cleanup: Callable[[], None],
) -> None:
    """Run a borrowed Retriever cleanup before its CLI lease is released."""
    key = _build_lock_key(kb_dir)
    token = (os.getpid(), threading.get_ident(), key)
    with _BUILD_LOCK_REGISTRY_GUARD:
        session = _BORROWABLE_READ_SESSIONS.get(token)
        if session is None:
            raise RuntimeError("no borrowable knowledge-base read session is active")
        session["cleanups"].append(cleanup)


def _windows_local_app_data() -> Optional[Path]:
    """Resolve Windows LocalAppData through the Known Folder API."""
    if os.name != "nt":
        return None
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        class GUID(ctypes.Structure):
            _fields_ = (
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            )

        folder_id = GUID(
            0xF1B32785,
            0x6FBA,
            0x4FCF,
            (ctypes.c_ubyte * 8)(
                0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91
            ),
        )
        raw_path = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        )
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(raw_path)
        )
        if result != 0 or not raw_path.value:
            return None
        try:
            return Path(raw_path.value)
        finally:
            ole32 = ctypes.windll.ole32
            ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
            ole32.CoTaskMemFree.restype = None
            ole32.CoTaskMemFree(ctypes.cast(raw_path, ctypes.c_void_p))
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def knowledge_base_lock_root() -> Path:
    """Return the stable per-user lock root without touching the filesystem."""
    user_suffix = str(os.getuid()) if hasattr(os, "getuid") else "current-user"
    if os.name == "nt":
        # TEMP/TMP may differ between launchers for the same Windows account;
        # LocalAppData is a stable OS-known rendezvous independent of them.
        base = _windows_local_app_data()
        if base is None:
            base = Path.home() / ".openmobius"
        return base / "OpenMobius" / "build-locks"
    # Deliberately bypass TMPDIR/TMP/TEMP. Two shells that point those at
    # different scratch directories must still serialize the same KB.
    return Path("/tmp") / f"openmobius-build-locks-{user_suffix}"


def _build_lock_directory() -> Path:
    directory = knowledge_base_lock_root()
    try:
        # Do not attempt mkdir against an already initialized root. Read-only
        # agent sandboxes may permit stat/open while rejecting every write-like
        # syscall, including mkdir(..., exist_ok=True).
        try:
            details = directory.stat()
        except FileNotFoundError:
            try:
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                if _is_read_only_policy_error(exc):
                    raise _ReadOnlyLockInfrastructureUnavailable(
                        "build-lock directory is not initialized and cannot be "
                        f"created: {directory}: {exc}"
                    ) from exc
                raise BuildLockUnavailable(
                    f"build-lock directory is unavailable: {directory}: {exc}"
                ) from exc
            details = directory.stat()
        if directory.is_symlink() or not stat.S_ISDIR(details.st_mode):
            raise BuildLockUnavailable(
                f"unsafe build-lock directory: {directory}"
            )
        if os.name != "nt" and (
            details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise BuildLockUnavailable(
                f"build-lock directory is not private to this user: {directory}"
            )
    except BuildLockUnavailable:
        raise
    except OSError as exc:
        raise BuildLockUnavailable(
            f"build-lock directory is unavailable: {directory}: {exc}"
        ) from exc
    return directory


def _open_lock_file(lock_path: Path, mode: str) -> int:
    """Open a stable lock inode without requesting writes for POSIX readers."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)

    if mode == "read":
        # POSIX flock permits a shared lease on an O_RDONLY descriptor.
        # Windows msvcrt locking still requires O_RDWR, but an absent first-use
        # file must be distinguished from an existing file that is unsafe or
        # inaccessible.
        read_flags = os.O_RDWR if _uses_windows_locking() else os.O_RDONLY
        try:
            return os.open(lock_path, read_flags | nofollow)
        except FileNotFoundError:
            # A writable first reader may initialize the rendezvous file. A
            # read-only sandbox instead gets a precise setup error, not a
            # false report that another operation owns the lock.
            initializer: Optional[int] = None
            try:
                initializer = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                )
                os.write(initializer, b"\0")
                os.fsync(initializer)
            except FileExistsError:
                # Another initializer won the race; open its stable inode.
                pass
            except OSError as exc:
                if _is_read_only_policy_error(exc):
                    raise _ReadOnlyLockInfrastructureUnavailable(
                        "knowledge-base lock infrastructure is not initialized "
                        f"and cannot be created: {lock_path}: {exc}"
                    ) from exc
                raise BuildLockUnavailable(
                    f"knowledge-base lock file cannot be initialized: "
                    f"{lock_path}: {exc}"
                ) from exc
            finally:
                if initializer is not None:
                    os.close(initializer)
            try:
                return os.open(lock_path, read_flags | nofollow)
            except OSError as exc:
                raise BuildLockUnavailable(
                    f"knowledge-base lock file is unavailable: {lock_path}: {exc}"
                ) from exc
        except OSError as exc:
            raise BuildLockUnavailable(
                f"knowledge-base lock file is unavailable: {lock_path}: {exc}"
            ) from exc

    try:
        return os.open(lock_path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
    except OSError as exc:
        raise BuildLockUnavailable(
            f"knowledge-base lock file is unavailable: {lock_path}: {exc}"
        ) from exc


def _lock_file_descriptor(descriptor: int, mode: str) -> None:
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl  # noqa: PLC0415

        operation = fcntl.LOCK_SH if mode == "read" else fcntl.LOCK_EX
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)


def _unlock_file_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415

        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def knowledge_base_build_lock(kb_dir: Path, *, mode: str = "write"):
    """Hold a crash-released generation lease for a complete KB operation.

    POSIX readers share the OS lock; writers are exclusive. Windows uses an
    exclusive byte-range lock for both roles because ``msvcrt`` has no shared
    lock primitive. Same-role acquisition is re-entrant, while read/write role
    changes fail immediately (including read-to-write in the same thread).
    The lock file is stable and external to the skill payload, so an active
    inode is never replaced by installer mirroring or package creation.
    """
    if mode not in {"read", "write"}:
        raise ValueError(f"unsupported knowledge-base lock mode: {mode}")
    bundle_marker = Path(kb_dir) / READ_ONLY_BUNDLE_MARKER
    bundle_marker_present = _path_entry_present(bundle_marker)
    if bundle_marker_present and mode == "write":
        raise BuildLockUnavailable(
            "knowledge base is an immutable read-only bundle: "
            f"{Path(kb_dir).resolve()}"
        )
    key = _build_lock_key(kb_dir)
    process_id = os.getpid()
    thread_id = threading.get_ident()
    descriptor: Optional[int] = None
    with _BUILD_LOCK_REGISTRY_GUARD:
        active = _ACTIVE_BUILD_LOCKS.get(key)
        if active is not None:
            if active["pid"] != process_id:
                raise BuildLockUnavailable(
                    f"another operation is active for knowledge base: "
                    f"{Path(kb_dir).resolve()}"
                )
            if active["mode"] != mode:
                raise BuildLockUnavailable(
                    "cannot change a knowledge-base generation lease from "
                    f"{active['mode']} to {mode}: {Path(kb_dir).resolve()}"
                )
            if mode == "write" and thread_id not in active["owners"]:
                raise BuildLockUnavailable(
                    f"another operation is active for knowledge base: "
                    f"{Path(kb_dir).resolve()}"
                )
            active["owners"][thread_id] = (
                active["owners"].get(thread_id, 0) + 1
            )
        else:
            static_lease = False
            try:
                lock_path = _build_lock_directory() / f"{key}.lock"
                descriptor = _open_lock_file(lock_path, mode)
            except _ReadOnlyLockInfrastructureUnavailable as init_error:
                if not (mode == "read" and bundle_marker_present):
                    raise
                # Constrained-host bundles have no writer or mutable index.
                # Validate immediately before creating the in-process lease;
                # compact_v2 validates its loaded in-memory snapshot again.
                if not _validate_read_only_bundle(kb_dir):
                    raise init_error
                static_lease = True

            try:
                if static_lease:
                    _ACTIVE_BUILD_LOCKS[key] = {
                        "pid": process_id,
                        "mode": mode,
                        "owners": {thread_id: 1},
                        "descriptor": None,
                        "static_bundle": True,
                    }
                    descriptor = None
                    lock_path = Path(kb_dir) / READ_ONLY_BUNDLE_MARKER
                else:
                    assert descriptor is not None
                    lock_stat = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(lock_stat.st_mode)
                        or lock_stat.st_nlink != 1
                    ):
                        raise OSError(
                            f"unsafe knowledge-base lock file: {lock_path}"
                        )
                    if lock_stat.st_size == 0 and not (
                        os.name != "nt" and mode == "read"
                    ):
                        os.write(descriptor, b"\0")
                        os.fsync(descriptor)
            except BuildLockUnavailable:
                if descriptor is not None:
                    os.close(descriptor)
                raise
            except OSError as exc:
                if descriptor is not None:
                    os.close(descriptor)
                raise BuildLockUnavailable(
                    f"unsafe knowledge-base lock file: {lock_path}: {exc}"
                ) from exc
            if not static_lease:
                try:
                    _lock_file_descriptor(descriptor, mode)
                except OSError as exc:
                    os.close(descriptor)
                    descriptor = None
                    raise BuildLockUnavailable(
                        f"another operation is active for knowledge base: "
                        f"{Path(kb_dir).resolve()}"
                    ) from exc
                try:
                    if bundle_marker_present:
                        _validate_read_only_bundle(kb_dir)
                except BaseException:
                    try:
                        _unlock_file_descriptor(descriptor)
                    finally:
                        os.close(descriptor)
                    descriptor = None
                    raise
                _ACTIVE_BUILD_LOCKS[key] = {
                    "pid": process_id,
                    "mode": mode,
                    "owners": {thread_id: 1},
                    "descriptor": descriptor,
                    "static_bundle": False,
                }
    try:
        yield
    finally:
        with _BUILD_LOCK_REGISTRY_GUARD:
            active = _ACTIVE_BUILD_LOCKS.get(key)
            if (
                active is None
                or active["pid"] != process_id
                or thread_id not in active["owners"]
            ):
                raise RuntimeError("knowledge-base build-lock ownership was lost")
            active["owners"][thread_id] -= 1
            if active["owners"][thread_id] == 0:
                del active["owners"][thread_id]
            if not active["owners"]:
                descriptor = active["descriptor"]
                if descriptor is None and active.get("static_bundle"):
                    del _ACTIVE_BUILD_LOCKS[key]
                else:
                    try:
                        _unlock_file_descriptor(descriptor)
                    finally:
                        try:
                            os.close(descriptor)
                        finally:
                            del _ACTIVE_BUILD_LOCKS[key]


@contextmanager
def knowledge_base_read_session(kb_dir: Path):
    """Hold and explicitly lend one read lease to a synchronous CLI call.

    Retriever instances normally own independent re-entrant leases. The CLI
    instead needs one lease around parsing-derived fast paths, Retriever use,
    canonical-card hydration and serialization. This scoped token lets those
    internal Retrievers borrow that outer lease without confusing it with a
    separately owned Retriever in the same thread.
    """
    key = _build_lock_key(kb_dir)
    token = (os.getpid(), threading.get_ident(), key)
    with knowledge_base_build_lock(kb_dir, mode="read"):
        with _BUILD_LOCK_REGISTRY_GUARD:
            session = _BORROWABLE_READ_SESSIONS.setdefault(
                token, {"depth": 0, "cleanups": []}
            )
            session["depth"] += 1
        try:
            yield
        finally:
            cleanups: list[Callable[[], None]] = []
            with _BUILD_LOCK_REGISTRY_GUARD:
                session = _BORROWABLE_READ_SESSIONS.get(token)
                if session is None:
                    raise RuntimeError(
                        "knowledge-base read-session ownership was lost"
                    )
                session["depth"] -= 1
                if session["depth"] == 0:
                    cleanups = list(reversed(session["cleanups"]))
                    del _BORROWABLE_READ_SESSIONS[token]
                else:
                    cleanups = []
            for cleanup in cleanups:
                try:
                    cleanup()
                except Exception as exc:  # noqa: BLE001 - release all resources
                    # A backend close failure must not skip other close hooks
                    # or leak the OS lease forever. The eventual writer still
                    # retains its own atomic/rollback error boundary.
                    import logging  # noqa: PLC0415

                    logging.getLogger(__name__).warning(
                        "knowledge-base reader cleanup failed: %s", exc
                    )
