import os
import unittest
from pathlib import Path


class WorkspacePathContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project_root = Path(__file__).resolve().parents[1]

    def test_workspace_root_is_bash_workdir(self):
        from apitelegramchat.workspace_paths import workspace_root, workspace_workdir

        root = workspace_root(987654, "path-contract-test")
        workdir = workspace_workdir(987654, "path-contract-test")
        self.assertEqual(root.resolve(), workdir.resolve())
        self.assertEqual(workdir.name, "path-contract-test")

    def test_special_directories_are_workspace_children(self):
        from apitelegramchat.workspace_paths import (
            workspace_root,
            workspace_upload_root,
            workspace_download_root,
        )

        root = workspace_root(987655, "path-contract-test-2").resolve()
        upload = workspace_upload_root(987655, "path-contract-test-2").resolve()
        download = workspace_download_root(987655, "path-contract-test-2").resolve()
        self.assertEqual(upload.parent, root)
        self.assertEqual(download.parent, root)

    def test_public_present_files_contract_is_workspace_relative(self):
        source = (self.project_root / "src/apitelegramchat/search_engine.py").read_text(encoding="utf-8")
        self.assertIn("workspace-relative paths", source)
        self.assertIn("upload/out.txt", source)
        self.assertNotIn("relative to \\\"upload/\\\"", source)

    def test_prompt_uses_workspace_relative_present_path(self):
        source = (self.project_root / "src/apitelegramchat/ai_handlers.py").read_text(encoding="utf-8")
        self.assertIn("工作区相对路径", source)
        self.assertIn("upload/结果.docx", source)


if __name__ == "__main__":
    unittest.main()

