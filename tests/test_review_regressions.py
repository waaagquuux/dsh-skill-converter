#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：朋友那份 review 里的 24 个反例，这里逐条钉住。

跑法：python tests/test_review_regressions.py
（只用标准库；CI 里在打包前跑一遍）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "skill-converter" / "convert.py"


def skill_text(name: str, body: str = "正文。\n", front_extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: 测试用技能 {name}\n{front_extra}---\n{body}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-retest-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.skills = self.tmp / "skills"
        self.skills.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- helpers
    def run_cli(self, *args):
        env = dict(os.environ)
        env["DSH_SKILL_HOME"] = str(self.home)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run([sys.executable, str(ENGINE)] + [str(a) for a in args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env)

    def make_skill(self, root: Path, dirname: str, body: str = "正文。\n",
                   name: str | None = None, front_extra: str = "") -> Path:
        d = root / dirname
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(skill_text(name or dirname, body, front_extra), encoding="utf-8")
        return d

    def install(self, src: Path, *extra):
        p = self.run_cli(src, "--install", "--skills-dir", self.skills, "--no-report", *extra)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p


class UndoTests(Base):
    def test_undo_after_user_edit_refuses_and_keeps_edit(self):
        self.make_skill(self.skills, "demo", "旧版本\n")
        pkg = self.tmp / "pkg"
        self.make_skill(pkg, "demo", "新版本\n")
        self.install(pkg)
        target = self.skills / "demo" / "SKILL.md"
        target.write_text("我自己改的\n", encoding="utf-8")

        p = self.run_cli("--undo")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("被改过", p.stderr + p.stdout)
        self.assertEqual(target.read_text(encoding="utf-8"), "我自己改的\n")
        self.assertTrue(target.exists())

    def test_undo_force_keeps_current_copy(self):
        pkg = self.tmp / "pkg"
        self.make_skill(pkg, "demo", "v1\n")
        self.install(pkg)
        (self.skills / "demo" / "SKILL.md").write_text("手改\n", encoding="utf-8")
        p = self.run_cli("--undo", "--force")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        kept = list((self.home / "output").glob("_undo_keep_*"))
        self.assertTrue(kept, "force 撤销必须先把当前版本另存一份")

    def test_undo_missing_backup_does_not_delete_destination(self):
        self.make_skill(self.skills, "demo", "旧\n")
        pkg = self.tmp / "pkg"
        self.make_skill(pkg, "demo", "新\n")
        self.install(pkg)
        backups = sorted(self.home.glob("_backup_*"))
        self.assertTrue(backups)
        shutil.rmtree(backups[-1] / "demo")     # 备份里的旧版本被人删了，manifest 还在

        p = self.run_cli("--undo")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertTrue((self.skills / "demo" / "SKILL.md").exists(),
                        "备份缺失时绝不能先把当前版本删掉")

    def test_undo_deletes_added_skill(self):
        pkg = self.tmp / "pkg"
        self.make_skill(pkg, "fresh")
        self.install(pkg)
        self.assertTrue((self.skills / "fresh").exists())
        p = self.run_cli("--undo")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((self.skills / "fresh").exists())


class BatchTests(Base):
    def test_duplicate_names_rejected(self):
        pkg = self.tmp / "pkg"
        a = pkg / "a" / "demo"
        b = pkg / "b" / "demo"
        for d in (a, b):
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(skill_text("demo"), encoding="utf-8")
        p = self.run_cli(pkg, "--all", "--install", "--skills-dir", self.skills, "--no-report")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("同名", p.stdout + p.stderr)
        self.assertFalse((self.skills / "demo").exists())

    def test_two_batches_do_not_share_backup_dir(self):
        p1 = self.tmp / "p1"
        p2 = self.tmp / "p2"
        self.make_skill(p1, "one")
        self.make_skill(p2, "two")
        self.install(p1)
        self.install(p2)
        backups = sorted(self.home.glob("_backup_*"))
        self.assertEqual(len(backups), 2, "同秒两批必须各占一个备份目录")
        for b in backups:
            self.assertTrue((b / "manifest.json").exists())


class CompressionTests(Base):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / "skill-converter"))
        import importlib
        import convert
        self.c = importlib.reload(convert)

    def test_conservative_keeps_everything_that_matters(self):
        body = ("# 说明\n\n必须得到客户批准才能发送。\n\n"
                "运行 `tool --dry-run --apply` 查看。\n\n"
                "1. 先备份\n2. 再执行\n3. 检查结果\n\n"
                "```bash\nfor i in 1 2 3 4 5 6 7 8 9 10; do echo $i; done\n```\n")
        raw = skill_text("demo", body, "disable-model-invocation: true\n")
        packed, stats = self.c.compress_text(raw)
        ok, checks = self.c.verify_compact(raw, packed)
        blocking = [c for c in checks if not c["ok"] and c["label"] != "确实更省"]
        self.assertFalse(blocking, blocking)
        self.assertIn("--dry-run", packed)
        self.assertIn("必须得到客户批准", packed)

    def test_semantic_reversal_rejected(self):
        raw = skill_text("demo", "MUST keep all customer records for 7 years.\n")
        bad = skill_text("demo", "MUST delete all customer records immediately.\n")
        ok, checks = self.c.verify_compact(raw, bad)
        self.assertFalse(ok)
        self.assertTrue(any(not c["ok"] and "硬性" in c["label"] for c in checks))

    def test_flag_prefix_false_negative(self):
        raw = skill_text("demo", "使用 `--name` 参数。\n")
        bad = skill_text("demo", "使用 `--namespace` 参数。\n")
        ok, checks = self.c.verify_compact(raw, bad)
        self.assertFalse(ok)
        self.assertTrue(any(not c["ok"] and "参数" in c["label"] for c in checks))

    def test_frontmatter_control_field_change_rejected(self):
        raw = skill_text("demo", "正文。\n", "disable-model-invocation: true\n")
        bad = skill_text("demo", "正文。\n")
        ok, checks = self.c.verify_compact(raw, bad)
        self.assertFalse(ok)
        self.assertTrue(any(not c["ok"] and "frontmatter" in c["label"] for c in checks))

    def test_code_block_change_rejected(self):
        raw = skill_text("demo", "```bash\nrm -rf /tmp/x\n```\n")
        bad = skill_text("demo", "```bash\nrm -rf /\n```\n")
        ok, checks = self.c.verify_compact(raw, bad)
        self.assertFalse(ok)

    def test_aggressive_truncation_is_blocked(self):
        code = "\n".join(f"echo step{i}" for i in range(1, 21))
        raw = skill_text("demo", f"```bash\n{code}\n```\n")
        packed, stats = self.c.compress_text(raw, aggressive=True)
        ok, checks = self.c.verify_compact(raw, packed)
        self.assertFalse(ok, "截断代码块必须被结构初筛拦下")


class ParseTests(Base):
    def test_yaml_inline_comment_and_bool(self):
        sys.path.insert(0, str(ROOT / "skill-converter"))
        import importlib
        import convert
        self.c = importlib.reload(convert)
        fm, err = self.c.parse_frontmatter(
            "---\nname: demo\ndescription: 说明\ndisable-model-invocation: true  # 需要显式调用\n---\n正文\n")
        self.assertEqual(err, "")
        self.assertIs(fm["disable-model-invocation"], True)
        self.assertEqual(fm["description"], "说明")

    def test_yaml_quoted_and_nested(self):
        sys.path.insert(0, str(ROOT / "skill-converter"))
        import importlib
        import convert
        self.c = importlib.reload(convert)
        fm, _ = self.c.parse_frontmatter(
            "---\nname: demo\ndescription: \"含: 冒号和 # 井号\"\nlicense: MIT\nmetadata:\n  version: 2\n---\n")
        self.assertEqual(fm["description"], "含: 冒号和 # 井号")
        self.assertEqual(fm["metadata"], {"version": 2})

    def test_github_ref_candidates_support_slashed_branch(self):
        sys.path.insert(0, str(ROOT / "skill-converter"))
        import importlib
        import convert
        self.c = importlib.reload(convert)
        owner, repo, parts = self.c.parse_github(
            "https://github.com/o/r/tree/feature/new/skills/demo")
        self.assertEqual((owner, repo), ("o", "r"))
        self.assertEqual(parts[0], "feature")
        cands = self.c._github_candidates(parts)
        self.assertIn(("feature/new", "skills/demo"), cands)

    def test_github_tag_url(self):
        sys.path.insert(0, str(ROOT / "skill-converter"))
        import importlib
        import convert
        self.c = importlib.reload(convert)
        _, _, parts = self.c.parse_github("https://github.com/o/r/releases/tag/v1.2.3")
        self.assertEqual(parts, ["v1.2.3"])


class InstallTests(Base):
    def test_flat_zip_normalizes_to_frontmatter_name(self):
        z = self.tmp / "flat.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("SKILL.md", skill_text("demo-flat"))
        p = self.run_cli(z, "--install", "--skills-dir", self.skills, "--no-report")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.skills / "demo-flat" / "SKILL.md").exists())

    def test_invalid_portable_name_rejected(self):
        pkg = self.tmp / "pkg"
        self.make_skill(pkg, "Bad_Name")
        p = self.run_cli(pkg, "--install", "--skills-dir", self.skills, "--no-report")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertFalse((self.skills / "Bad_Name").exists())

    def test_zip_traversal_still_rejected(self):
        z = self.tmp / "evil.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../evil.txt", "x")
        p = self.run_cli(z, "--dry-run", "--no-report")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)

    def test_self_install_ships_engine(self):
        p = self.run_cli("--self-install", "--skills-dir", self.skills)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        d = self.skills / "skill-converter"
        self.assertTrue((d / "convert.py").exists(), "引擎必须跟着技能一起装")
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("自安装", text)
        self.assertNotIn("E:\\dsh-skill", text, "不能把作者的绝对路径带出去")
        # 头部写的引擎路径必须是最终安装位置，不能是临时目录
        self.assertIn(str(d / "convert.py"), text)
        self.assertNotIn(".dsh-self-", text, "不能把 staging 临时目录写进技能说明")
        self.assertNotIn(".dsh-stage-", text, "不能把 staging 临时目录写进技能说明")

    def test_readonly_skills_dir_gives_actionable_error(self):
        # 用一个不可写的“目录”模拟受限环境：拿一个已存在的文件当目录
        bad = self.tmp / "not-a-dir"
        bad.write_text("x", encoding="utf-8")
        pkg = self.tmp / "pkg"
        self.make_skill(pkg, "demo")
        p = self.run_cli(pkg, "--install", "--skills-dir", bad, "--no-report")
        self.assertIn(p.returncode, (3, 4), p.stdout + p.stderr)
        self.assertIn("写", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
