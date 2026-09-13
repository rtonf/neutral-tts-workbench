import hashlib
import io
import json
import tarfile
import unittest
import zipfile

from neutral_tts.source_wheels import source_wheel


class SourceWheelTests(unittest.TestCase):
    def recipe(self, data, format="zip"):
        return dict(size=len(data), sha256=hashlib.sha256(data).hexdigest(), format=format,
                    distribution="example", version="1.0+local.1", packages=["example"],
                    license_file="LICENSE", license="MIT", dependencies=["dependency>=1"],
                    url="https://example.org/fixed-source", revision="fixed")

    def archive(self, extra=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            for name, data in {"root/example/__init__.py": b"VALUE = 1\n",
                               "root/LICENSE": b"License fixture", "root/setup.py": b"raise RuntimeError()",
                               **(extra or {})}.items():
                info = zipfile.ZipInfo("placeholder")
                info.filename = name  # Preserve malicious separators on Windows.
                z.writestr(info, data)
        return stream.getvalue()

    def test_rebuild_from_complete_lock_preserves_code_license_and_identity(self):
        data = self.archive()
        recipe = self.recipe(data)
        name, wheel = source_wheel(data, recipe)
        recipe.update(wheel_file=name, wheel_sha256=hashlib.sha256(wheel).hexdigest())
        self.assertEqual((name, wheel), source_wheel(data, recipe))
        with zipfile.ZipFile(io.BytesIO(wheel)) as z:
            self.assertEqual(z.read("example/__init__.py"), b"VALUE = 1\n")
            self.assertNotIn("setup.py", z.namelist())
            license_name = next(n for n in z.namelist() if n.endswith("/licenses/LICENSE"))
            self.assertEqual(z.read(license_name), b"License fixture")
            record = next(n for n in z.namelist() if n.endswith("/RECORD"))
            self.assertEqual(len(z.read(record).splitlines()), len(z.namelist()))

    def test_hash_paths_links_and_member_bounds_fail_closed(self):
        data = self.archive()
        recipe = self.recipe(data)
        recipe["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            source_wheel(data, recipe)
        for name in ("root/../escape.py", "root/example" + chr(92) + "evil.py", "root/C:evil.py"):
            data = self.archive({name: b"unsafe"})
            with self.assertRaises(ValueError):
                source_wheel(data, self.recipe(data))
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as z:
            link = tarfile.TarInfo("root/example/__init__.py")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../escape"
            z.addfile(link)
        data = stream.getvalue()
        with self.assertRaises(ValueError):
            source_wheel(data, self.recipe(data, "tar.gz"))


if __name__ == "__main__":
    unittest.main()
