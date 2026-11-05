"""Execute the real browser chart logic with a tiny DOM, without broker requests."""
import shutil
import subprocess
import unittest
from pathlib import Path


class ChartRecoveryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for chart rendering checks')
    def test_recovery_chart_regressions(self):
        script=Path(__file__).with_name('chart_recovery.test.js')
        result=subprocess.run([shutil.which('node'),'--test',str(script)],capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)


if __name__=='__main__':unittest.main()
