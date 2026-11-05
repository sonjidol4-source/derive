import tempfile
import unittest
import zipfile
from pathlib import Path
from tr_schema import catalog


class InstalledSchemaTests(unittest.TestCase):
    def test_internal_english_trname_does_not_hide_investor_query(self):
        spec='[TRINFO]\nTRName=FOSTOCK\n[INPUT]\n@START_선옵투자자매매요청\n시장구분=0,1,1\n@END\n[OUTPUT]\n@START_선옵투자자매매=*,*,*\n외국인=0,20,1\n기관계=20,20,2\n@END'
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory)/'data'
            data.mkdir()
            with zipfile.ZipFile(data/'opt59999.enc','w') as archive:
                archive.writestr('opt59999.dat',spec.encode('cp949'))
            specs=catalog(directory)
        self.assertEqual(len(specs),1)
        self.assertEqual(specs[0]['title'],'선옵투자자매매')
        self.assertEqual(specs[0]['internal_name'],'FOSTOCK')
