import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from rest_feed import read_env


class LocalCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/'app.txt').write_text('test-app\n',encoding='utf-8-sig')
        (self.root/'secret.txt').write_text('test-secret\n',encoding='utf-8-sig')
        self.source=self.root/'local_credentials.json'
        self.source.write_text(json.dumps({'app_key_file':'app.txt','secret_key_file':'secret.txt'}),encoding='utf-8-sig')

    def tearDown(self):
        self.temp.cleanup()

    def test_reuses_existing_files_without_copying(self):
        with patch.dict(os.environ,{},clear=True):
            values=read_env(self.source)
        self.assertEqual(values['KIWOOM_APP_KEY'],'test-app')
        self.assertEqual(values['KIWOOM_SECRET_KEY'],'test-secret')
        self.assertEqual(len(list(self.root.iterdir())),3)

    def test_process_environment_keeps_precedence(self):
        with patch.dict(os.environ,{'KIWOOM_APP_KEY':'override'},clear=True):
            self.assertEqual(read_env(self.source)['KIWOOM_APP_KEY'],'override')

    def test_missing_file_reports_no_sensitive_content(self):
        (self.root/'secret.txt').unlink()
        with self.assertRaisesRegex(ValueError,'기존 키움 인증 파일을 읽을 수 없습니다'):
            read_env(self.source)

    def test_env_files_still_supported(self):
        source=self.root/'.env'
        source.write_text('KIWOOM_APPKEY=test-app\nKIWOOM_SECRETKEY=test-secret\n',encoding='utf-8')
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(read_env(source)['KIWOOM_SECRETKEY'],'test-secret')
