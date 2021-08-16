import hashlib

from .types import pathlike


def get_file_hash(filepath: pathlike, algorithm: str) -> str:
    hashobj = getattr(hashlib, algorithm)()
    with open(filepath, 'rb') as f:
        block = f.read(4096)
        while block:
            hashobj.update(block)
            block = f.read(4096)

    return hashobj.hexdigest()
