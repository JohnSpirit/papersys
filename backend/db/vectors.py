"""向量序列化工具：embeddings.vector BLOB <-> list[float]。

供 service 层（semantic_search）与 worker 层（embed_worker）共用，
避免 service 反向依赖 worker 层。
"""
import struct


def pack(vec: list[float]) -> bytes:
    """把 float 列表打包为 float32 小端 BLOB。"""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    """把 BLOB 解包回 float 列表（按 4 字节/float）。"""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))
