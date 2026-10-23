"""Local runtime files belong to local/, not to the primary server project."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from local.db import Database


class LocalPathTests(unittest.TestCase):
    def test_default_database_is_inside_local(self):
        with patch.dict(os.environ,{'VIEWER_DB':''}):
            self.assertEqual(Database().db_path.resolve(),Path(__file__).resolve().parents[1]/'viewer.db')

    def test_both_legacy_database_locations_are_protected(self):
        root=Path(__file__).resolve().parents[2]
        for path in (root/'claude.db',root/'local/claude.db'):
            with self.assertRaises(ValueError):Database(path)


if __name__=='__main__':unittest.main()
