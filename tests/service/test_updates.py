import base64
import importlib.util
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from test_control import ROOT

spec = importlib.util.spec_from_file_location("update_control", ROOT / "scripts/update-control.py")
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.area = Path(self.tmp.name)
        self.root = self.area / "app"
        (self.root / "config").mkdir(parents=True)
        (self.root / "scripts").mkdir()
        shutil.copyfile(
            ROOT / "scripts/verify-release.cjs", self.root / "scripts/verify-release.cjs"
        )
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        (self.root / "config/release-public.pem").write_bytes(public)

    def signed(self, manifest):
        raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
        return {
            "payload": base64.b64encode(raw).decode(),
            "signature": base64.b64encode(self.key.sign(raw)).decode(),
            "manifest": manifest,
        }

    def release(self, sequence, files):
        files = {
            "config/release-public.pem": (self.root / "config/release-public.pem").read_bytes(),
            **files,
        }
        archive = self.area / ("source-" + str(sequence) + ".zip")
        with zipfile.ZipFile(archive, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        manifest = {
            "schema": 1,
            "product": "aieyra-control",
            "version": str(sequence),
            "sequence": sequence,
            "source": {"sha256": update.hashed(archive), "size": archive.stat().st_size},
            "files": {
                name: {"sha256": update.hashlib.sha256(content).hexdigest(), "size": len(content)}
                for name, content in files.items()
            },
        }
        path = self.area / ("release-" + str(sequence) + ".json")
        update.write(path, self.signed(manifest))
        return path, archive

    def install(self, files):
        path, archive = self.release(1, files)
        shutil.copyfile(path, self.root / "config/release-baseline.json")
        for name, content in files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def test_three_way_keep_merge_apply_rollback_and_drift(self):
        self.install({"web/a.js": b"base", "web/local.js": b"stock", "web/gone.js": b"old"})
        (self.root / "web/a.js").write_bytes(b"local edit")
        (self.root / "web/local.js").write_bytes(b"local only")
        manifest, archive = self.release(
            2, {"web/a.js": b"new upstream", "web/local.js": b"stock", "web/add.js": b"new"}
        )
        work = self.area / "candidate"
        plan = update.prepare(self.root, manifest, archive, work)
        cats = {r["path"]: r["category"] for r in plan["files"]}
        self.assertEqual(cats["web/a.js"], "conflict")
        self.assertEqual(cats["web/local.js"], "local_only")
        with self.assertRaises(ValueError):
            update.apply(self.root, work, self.area / "data")
        plan["decision"] = "accept"
        for r in plan["files"]:
            if r["choice"] == "review":
                r["choice"] = "take"
        update.write(work / "plan.json", plan)
        (self.root / "web/a.js").write_bytes(b"later edit")
        with self.assertRaises(ValueError):
            update.apply(self.root, work, self.area / "data")
        (self.root / "web/a.js").write_bytes(b"local edit")
        merged = work / "merged/web/a.js"
        merged.parent.mkdir(parents=True)
        merged.write_bytes(b"reviewed combined")
        row = next(r for r in plan["files"] if r["path"] == "web/a.js")
        row["choice"] = "merge"
        row["merged_sha256"] = update.hashed(merged)
        update.write(work / "plan.json", plan)
        result = update.apply(self.root, work, self.area / "data")
        self.assertEqual(result["reload"], "interface")
        self.assertEqual((self.root / "web/local.js").read_bytes(), b"local only")
        self.assertFalse((self.root / "web/gone.js").exists())
        self.assertEqual((self.root / "web/a.js").read_bytes(), b"reviewed combined")
        update.restore(self.root, work)
        self.assertEqual((self.root / "web/a.js").read_bytes(), b"local edit")
        self.assertTrue((self.root / "web/gone.js").exists())

    def test_tamper_archive_traversal_downgrade_and_post_apply_edits(self):
        self.install({"web/a.js": b"base"})
        manifest, archive = self.release(2, {"web/a.js": b"new"})
        envelope = update.read(manifest)
        envelope["manifest"]["version"] = "forged"
        update.write(manifest, envelope)
        with self.assertRaises(ValueError):
            update.prepare(self.root, manifest, archive, self.area / "bad")
        manifest, archive = self.release(2, {"../outside": b"x"})
        with self.assertRaises(ValueError):
            update.prepare(self.root, manifest, archive, self.area / "traversal")
        manifest, archive = self.release(1, {"web/a.js": b"base"})
        with self.assertRaises(ValueError):
            update.prepare(self.root, manifest, archive, self.area / "old")

    def test_rollback_validates_all_backups_before_touching_any_file(self):
        self.install({"web/a.js": b"base-a", "web/b.js": b"base-b"})
        manifest, archive = self.release(2, {"web/a.js": b"new-a", "web/b.js": b"new-b"})
        work = self.area / "rollback"
        plan = update.prepare(self.root, manifest, archive, work)
        plan["decision"] = "accept"
        for r in plan["files"]:
            if r["choice"] == "review":
                r["choice"] = "take"
        update.write(work / "plan.json", plan)
        update.apply(self.root, work, self.area / "data")
        (self.root / "web/a.js").write_bytes(b"post-update edit")
        with self.assertRaisesRegex(ValueError, "post_update_local_changes"):
            update.restore(self.root, work)
        self.assertEqual((self.root / "web/b.js").read_bytes(), b"new-b")
        (self.root / "web/a.js").write_bytes(b"new-a")
        (work / "backup/web/b.js").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "backup_digest_mismatch"):
            update.restore(self.root, work)
        self.assertEqual((self.root / "web/a.js").read_bytes(), b"new-a")
        (work / "backup/web/b.js").write_bytes(b"base-b")
        update.restore(self.root, work)
        self.assertEqual((self.root / "web/a.js").read_bytes(), b"base-a")


if __name__ == "__main__":
    unittest.main()
