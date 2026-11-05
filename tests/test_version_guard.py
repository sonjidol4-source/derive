import tempfile
import unittest
from pathlib import Path
from unittest import mock
import version_guard
from version_guard import VersionWatch,core_update_in,is_version_popup,verlog_path

POPUP='버전처리를 받으시려면 현재 실행 중인 OpenAPI를 사용하는 프로그램(KOA Studio포함)을 모두 종료하신 후 확인버튼을 눌러주시기 바랍니다.'


class VersionGuardTests(unittest.TestCase):
    def test_popup_requires_updater_process_and_text(self):
        self.assertTrue(is_version_popup('D:\\OpenAPI\\opstarter.exe',['',POPUP,'확인']))
        self.assertFalse(is_version_popup('python.exe',[POPUP]))
        self.assertFalse(is_version_popup('opstarter.exe',['로그인','확인']))

    def test_only_locked_core_files_trigger_exit(self):
        self.assertEqual(core_update_in('[21:46:25]:서버에 FileDown요구 [khopenapi.ocx]'),'khopenapi.ocx')
        self.assertEqual(core_update_in('[21:45:57]:DownFileInList CRC 다름 File[opcommapi.dll] LocalCrc[5c8c100c]'),'opcommapi.dll')
        # 로그인 창이 쓰는 보안 모듈·코드 파일 갱신은 이 프로세스 종료가 필요 없음.
        self.assertIsNone(core_update_in('[21:46:05]:서버에 FileDown요구 [skcommif.dll]\n[10:42:02]:DownFileInList CRC 다름 File[trcode.dat]'))
        self.assertIsNone(core_update_in(''))

    def test_watch_ignores_log_written_before_login(self):
        with tempfile.TemporaryDirectory() as d:
            log=verlog_path(d);log.parent.mkdir()
            log.write_bytes('[19:05:13]:서버에 FileDown요구 [opcomms.dll]\n'.encode('cp949'))
            watch=VersionWatch(d,lambda reason:None)
            self.assertIsNone(watch.check())
            with open(log,'ab') as f:f.write('[21:46:28]:서버에 FileDown요구 [opcomms.dll]\n'.encode('cp949'))
            self.assertIn('opcomms.dll',watch.check())
            self.assertIsNone(watch.check())

    def test_wait_covers_gap_between_starter_and_versionup(self):
        # opstarter 종료 → 몇 초 공백 → OPVersionUp 교체. 공백에서 바로 재실행하면 [183]이 난다.
        clock=[0.0];states=iter([[1],[1],[],[],[],[9],[9]]+[[]]*60)
        def sleep(s):clock[0]+=s
        with mock.patch.object(version_guard,'updater_running',lambda:next(states)), \
             mock.patch.object(version_guard.time,'sleep',sleep), \
             mock.patch.object(version_guard.time,'monotonic',lambda:clock[0]):
            self.assertTrue(version_guard.wait_updater(log=lambda s:None,quiet=30))
        self.assertGreaterEqual(clock[0],5+30)

    def test_versionup_popup_counts_as_updater(self):
        self.assertTrue(is_version_popup('OPVersionUp.exe',[POPUP]))

    def test_missing_log_is_not_an_update(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(VersionWatch(Path(d),lambda reason:None).check())


if __name__=='__main__':unittest.main()
