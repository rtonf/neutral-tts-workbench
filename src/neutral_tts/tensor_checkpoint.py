"""Extract contiguous float tensors as data, without executing pickle objects.

This deliberately narrow protocol-2 reader never imports a GLOBAL, calls REDUCE,
constructs NEWOBJ classes, or invokes BUILD/setstate. Configuration objects are
inert records and discarded. Unsupported opcodes, globals and tensor layouts
fail closed. The runtime still refuses unrestricted checkpoint loading.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import pickletools
import struct
import zipfile
from dataclasses import dataclass


class TensorCheckpointError(ValueError):
    pass


@dataclass(frozen=True)
class _Global:
    name: str


@dataclass
class _Record:
    kind: str
    arguments: object
    state: object = None


_GLOBALS = {
    "__builtin__ dict", "__builtin__ list", "__builtin__ long", "typing Any",
    "collections OrderedDict", "collections defaultdict",
    "omegaconf.base ContainerMetadata", "omegaconf.base Metadata",
    "omegaconf.dictconfig DictConfig", "omegaconf.listconfig ListConfig", "omegaconf.nodes AnyNode",
    "torch FloatStorage", "torch._utils _rebuild_tensor_v2",
}
_MARK = object()
MAX_CHECKPOINT_BYTES = 128 * 1024**2


def _data_tree(data):
    if len(data) > 1024 * 1024:
        raise TensorCheckpointError("The tensor index is too large.")
    stack, memo = [], {}
    def pop_group():
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] is _MARK:
                items = stack[index + 1:]
                del stack[index:]
                return items
        raise TensorCheckpointError("A container marker is missing.")

    def insert(mapping, values):
        if type(mapping) is not dict or len(values) % 2:
            raise TensorCheckpointError("A map operation is invalid.")
        for key, value in zip(values[::2], values[1::2]):
            valid_key = type(key) in {str, int} or (type(key) is tuple and len(key) <= 32 and all(type(v) in {str, int} for v in key))
            if not valid_key or key in mapping:
                raise TensorCheckpointError("A map key is unsupported or duplicated.")
            mapping[key] = value

    try:
        for count, (operation, argument, position) in enumerate(pickletools.genops(data)):
            if count > 100000 or len(stack) > 8192 or len(memo) > 32768:
                raise TensorCheckpointError("The tensor index exceeds its complexity bound.")
            name = operation.name
            if name == "PROTO":
                if position != 0 or argument != 2:
                    raise TensorCheckpointError("Only the fixed protocol-2 format is supported.")
            elif name == "MARK":
                stack.append(_MARK)
            elif name in {"EMPTY_DICT", "EMPTY_LIST", "EMPTY_TUPLE"}:
                stack.append({} if name == "EMPTY_DICT" else [] if name == "EMPTY_LIST" else ())
            elif name in {"BINUNICODE", "BININT", "BININT1", "BININT2", "BINFLOAT"}:
                if isinstance(argument, str) and len(argument) > 16384:
                    raise TensorCheckpointError("An index string is too large.")
                stack.append(argument)
            elif name in {"NEWTRUE", "NEWFALSE", "NONE"}:
                stack.append(True if name == "NEWTRUE" else False if name == "NEWFALSE" else None)
            elif name in {"BINPUT", "LONG_BINPUT"}:
                if argument in memo or not 0 <= argument < 32768:
                    raise TensorCheckpointError("An index memo is invalid.")
                memo[argument] = stack[-1]
            elif name in {"BINGET", "LONG_BINGET"}:
                stack.append(memo[argument])
            elif name == "GLOBAL":
                if argument not in _GLOBALS:
                    raise TensorCheckpointError("The index names an unsupported global.")
                stack.append(_Global(argument))
            elif name == "TUPLE":
                stack.append(tuple(pop_group()))
            elif name in {"TUPLE1", "TUPLE2", "TUPLE3"}:
                number = int(name[-1])
                items = tuple(stack[-number:]); del stack[-number:]
                stack.append(items)
            elif name in {"NEWOBJ", "REDUCE"}:
                arguments, reference = stack.pop(), stack.pop()
                if not isinstance(reference, _Global) or type(arguments) is not tuple:
                    raise TensorCheckpointError("An object description is invalid.")
                if name == "REDUCE" and reference.name in {"collections OrderedDict", "collections defaultdict"}:
                    if reference.name.endswith("OrderedDict") and arguments:
                        raise TensorCheckpointError("Only empty ordered-map construction is supported.")
                    stack.append({})
                else:
                    # Data only: no Python callable or imported class is resolved.
                    stack.append(_Record(reference.name, arguments))
            elif name == "BUILD":
                state = stack.pop()
                if not isinstance(stack[-1], _Record):
                    raise TensorCheckpointError("An object state target is unsupported.")
                stack[-1].state = state
            elif name == "SETITEMS":
                values = pop_group(); insert(stack[-1], values)
            elif name == "SETITEM":
                value, key = stack.pop(), stack.pop(); insert(stack[-1], [key, value])
            elif name in {"APPEND", "APPENDS"}:
                values = [stack.pop()] if name == "APPEND" else pop_group()
                if type(stack[-1]) is not list:
                    raise TensorCheckpointError("An array target is unsupported.")
                stack[-1].extend(values)
            elif name == "BINPERSID":
                stack.append(_Record("storage", stack.pop()))
            elif name == "STOP":
                if len(stack) != 1 or type(stack[0]) is not dict or position + 1 != len(data):
                    raise TensorCheckpointError("The tensor index has a malformed root.")
                return stack[0]
            else:
                raise TensorCheckpointError("The tensor index contains an unsupported opcode.")
    except (IndexError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, TensorCheckpointError):
            raise
        raise TensorCheckpointError("The tensor index is malformed.") from error
    raise TensorCheckpointError("The tensor index is incomplete.")


def extract_safetensors(source: bytes, *, expected_sha256: str) -> bytes:
    """Return a new tensor-only artifact; the original checkpoint is not executed."""
    if not 0 < len(source) <= MAX_CHECKPOINT_BYTES or hashlib.sha256(source).hexdigest() != expected_sha256:
        raise TensorCheckpointError("The source differs from its pinned checkpoint.")
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        infos = archive.infolist()
        if not 1 <= len(infos) <= 2048 or len({i.filename for i in infos}) != len(infos):
            raise TensorCheckpointError("The checkpoint archive inventory is invalid.")
        if any(i.compress_type != zipfile.ZIP_STORED or i.flag_bits & 1 or i.file_size > MAX_CHECKPOINT_BYTES for i in infos) or sum(i.file_size for i in infos) > MAX_CHECKPOINT_BYTES:
            raise TensorCheckpointError("The checkpoint archive is compressed or oversized.")
        indexes = [i.filename for i in infos if i.filename.endswith("/data.pkl")]
        if len(indexes) != 1:
            raise TensorCheckpointError("The checkpoint tensor index is ambiguous.")
        prefix = indexes[0][:-len("data.pkl")]
        if any(not i.filename.startswith(prefix) or any(p in {"", ".", ".."} for p in i.filename.split("/")) for i in infos):
            raise TensorCheckpointError("The checkpoint contains unexpected paths.")
        if prefix + "byteorder" in archive.namelist() and archive.read(prefix + "byteorder") != b"little":
            raise TensorCheckpointError("Only little-endian tensor storage is supported.")
        tree = _data_tree(archive.read(indexes[0]))
        tensors = tree.get("model")
        if tensors is None and type(tree.get("best_state")) is dict:
            tensors = tree["best_state"].get("model")
        if type(tensors) is not dict or not 1 <= len(tensors) <= 2048:
            raise TensorCheckpointError("The checkpoint has no supported tensor state.")
        header, payload, offset = {}, [], 0
        for key, value in sorted(tensors.items()):
            if type(key) is not str or not 0 < len(key) <= 256 or key == "__metadata__":
                raise TensorCheckpointError("A tensor name is invalid.")
            if not isinstance(value, _Record) or value.kind != "torch._utils _rebuild_tensor_v2" or value.state is not None:
                raise TensorCheckpointError("The state contains a non-tensor value.")
            args = value.arguments
            if len(args) != 6:
                raise TensorCheckpointError("The tensor description is unsupported.")
            storage, start, shape, stride, gradient, hooks = args
            if not isinstance(storage, _Record) or storage.kind != "storage" or type(start) is not int or start < 0:
                raise TensorCheckpointError("The tensor storage is invalid.")
            if type(shape) is not tuple or type(stride) is not tuple or len(shape) != len(stride) or len(shape) > 8 or any(type(v) is not int or v < 0 for v in (*shape, *stride)):
                raise TensorCheckpointError("The tensor dimensions are invalid.")
            expected_stride = 1
            for dim, step in zip(reversed(shape), reversed(stride)):
                if step != expected_stride:
                    raise TensorCheckpointError("Only contiguous tensors are supported.")
                expected_stride *= dim
            descriptor = storage.arguments
            if type(descriptor) is not tuple or len(descriptor) != 5:
                raise TensorCheckpointError("The storage description is invalid.")
            tag, dtype, storage_id, device, size = descriptor
            if tag != "storage" or dtype != _Global("torch FloatStorage") or type(storage_id) is not str or not storage_id.isascii() or not storage_id.isdecimal() or type(size) is not int or not 0 <= size <= MAX_CHECKPOINT_BYTES // 4:
                raise TensorCheckpointError("Only bounded float32 tensor storage is supported.")
            count = math.prod(shape)
            if start + count > size or type(gradient) is not bool or hooks != {}:
                raise TensorCheckpointError("The tensor exceeds storage or has unsupported hooks.")
            raw = archive.read(prefix + "data/" + storage_id)
            if len(raw) != size * 4 or offset + count * 4 > MAX_CHECKPOINT_BYTES:
                raise TensorCheckpointError("The tensor data size is inconsistent.")
            chunk = raw[start * 4:(start + count) * 4]
            header[key] = {"dtype": "F32", "shape": list(shape), "data_offsets": [offset, offset + len(chunk)]}
            offset += len(chunk); payload.append(chunk)
        header["__metadata__"] = {"source_sha256": expected_sha256, "conversion": "static-protocol2-f32-v1"}
        encoded = json.dumps(header, ensure_ascii=True, separators=(",", ":")).encode()
        encoded += b" " * (-len(encoded) % 8)
        return struct.pack("<Q", len(encoded)) + encoded + b"".join(payload)
