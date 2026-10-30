"""Page-cache residency of every file under a directory, or evict those files from the cache.

Runs inside a throwaway container with the engine's data volume mounted read-only, so it
needs no root on the host:
  docker run --rm -v <volume>:/data:ro -v ./scripts/pagecache.py:/p.py:ro python:3.13-slim \
      python /p.py resident /data
resident: one JSON line per file {"path", "size", "resident"} (bytes).
evict:    drop each file's clean pages (posix_fadvise DONTNEED). Stop the engine first, so
          nothing is dirty or memory-mapped.
"""
import ctypes
import json
import os
import sys

libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                      ctypes.c_int, ctypes.c_long]
libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
PAGE = os.sysconf("SC_PAGE_SIZE")
PROT_READ, MAP_SHARED, MAP_FAILED = 1, 1, ctypes.c_void_p(-1).value


def resident(fd: int, size: int) -> int:
    # Mapping a file without touching it loads nothing; mincore reports which pages are cached.
    addr = libc.mmap(None, size, PROT_READ, MAP_SHARED, fd, 0)
    if addr == MAP_FAILED:
        raise OSError(ctypes.get_errno(), "mmap failed")
    try:
        vec = (ctypes.c_ubyte * ((size + PAGE - 1) // PAGE))()
        if libc.mincore(addr, size, vec) != 0:
            raise OSError(ctypes.get_errno(), "mincore failed")
        return sum(v & 1 for v in vec) * PAGE
    finally:
        libc.munmap(addr, size)


def main(mode: str, root: str) -> None:
    for d, _, files in os.walk(root):
        for name in files:
            path = os.path.join(d, name)
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError:
                continue  # sockets, files removed mid-walk
            try:
                size = os.fstat(fd).st_size
                if mode == "evict":
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                elif size:
                    print(json.dumps({"path": os.path.relpath(path, root), "size": size,
                                      "resident": resident(fd, size)}))
            finally:
                os.close(fd)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
