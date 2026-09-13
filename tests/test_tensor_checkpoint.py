import hashlib
import io
import json
import struct
import unittest
import zipfile

from neutral_tts.tensor_checkpoint import extract_safetensors, TensorCheckpointError, _data_tree


def text(value):
    raw = value.encode()
    return b"X" + struct.pack("<I", len(raw)) + raw


def index(*, shape=2, stride=1, global_name=b"torch._utils\n_rebuild_tensor_v2\n"):
    # Minimal tensor-only protocol-2 fixture. No external package or executable
    # pickle producer/consumer is needed for the security regression tests.
    return (b"\x80\x02}" + text("model") + b"}" + text("weight") + b"c" + global_name +
            b"((" + text("storage") + b"ctorch\nFloatStorage\n" + text("0") + text("cpu") +
            b"K\x02tQK\x00K" + bytes([shape]) + b"\x85K" + bytes([stride]) + b"\x85\x89}tRss.")


def checkpoint(pickle_index, *, compressed=False):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED) as out:
        out.writestr("archive/data.pkl", pickle_index)
        out.writestr("archive/data/0", struct.pack("<2f", 0.25, -0.75))
        out.writestr("archive/byteorder", "little")
    return stream.getvalue()


class TensorCheckpointTests(unittest.TestCase):
    def convert(self, data):
        return extract_safetensors(data, expected_sha256=hashlib.sha256(data).hexdigest())

    def test_data_only_conversion_preserves_exact_tensor_bytes(self):
        converted = self.convert(checkpoint(index()))
        size = struct.unpack("<Q", converted[:8])[0]
        header = json.loads(converted[8:8 + size])
        self.assertEqual(header["weight"], {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]})
        self.assertEqual(converted[8 + size:], struct.pack("<2f", 0.25, -0.75))

    def test_executable_global_and_unknown_opcode_are_rejected(self):
        for data in [index(global_name=b"os\nsystem\n"), b"\x80\x02cos\nsystem\n" + text("unexecuted") + b"\x85R.", b"\x80\x02\x82\x01."]:
            with self.assertRaises(TensorCheckpointError):
                self.convert(checkpoint(data))

    def test_shape_stride_hash_and_compression_are_checked(self):
        for data in [checkpoint(index(shape=3)), checkpoint(index(stride=2)), checkpoint(index(), compressed=True)]:
            with self.assertRaises(TensorCheckpointError):
                self.convert(data)
        with self.assertRaises(TensorCheckpointError):
            extract_safetensors(checkpoint(index()), expected_sha256="0" * 64)

    def test_truncation_duplicate_keys_and_extra_bytes_are_rejected(self):
        valid = index()
        for limit in range(len(valid)):
            with self.assertRaises(TensorCheckpointError):
                _data_tree(valid[:limit])
        with self.assertRaises(TensorCheckpointError):
            _data_tree(b"\x80\x02}(" + text("a") + b"K\x01" + text("a") + b"K\x02u.")
        with self.assertRaises(TensorCheckpointError):
            _data_tree(valid + b"extra")


if __name__ == "__main__":
    unittest.main()
